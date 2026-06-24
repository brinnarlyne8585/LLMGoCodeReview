
import tree_sitter_go as tsgo
from tree_sitter import Language, Parser, Node
from typing import List, Dict, Any, Optional

class TreeSitterAnalyzer:
    def __init__(self):
        try:
            ptr = tsgo.language()
            self.GO_LANGUAGE = Language(ptr, "go")
            self.parser = Parser()
            self.parser.set_language(self.GO_LANGUAGE)
        except Exception as e:
            raise

    def parse(self, code: str):
        try:
            return self.parser.parse(bytes(code, "utf8"))
        except Exception as e:
            raise

    def find_nodes_at_lines(self, tree, lines: List[int]) -> List[Node]:
        """
        Find all leaf nodes that strictly intersect the given lines.
        This does not walk upward and does not check node importance types.
        """
        if not lines:
            return []
            
        nodes = []
        root = tree.root_node
 
        line_set = set(lines)

        # Optimization: get min/max line numbers for pruning traversal.
        min_line = min(lines)
        max_line = max(lines)

        def visit(node):
            # Prune.
            if node.end_point[0] < min_line or node.start_point[0] > max_line:
                return

            # Treat leaves and interpreted_string_literal as atomic leaves.
            # Tree-sitter Go may expose children for interpreted_string_literal,
            # such as quotes, but we compare it as one unit.
            is_atomic_leaf = (node.type == 'interpreted_string_literal') or (node.child_count == 0)
            
            if is_atomic_leaf:
                # Check intersection: node range overlaps any valid line.
                # Leaves are usually single-line, but string literals may span lines.
                n_start = node.start_point[0]
                n_end = node.end_point[0]
                
                # Check whether any line in [n_start, n_end] exists in line_set.
                is_hit = False
                for l in range(n_start, n_end + 1):
                    if l in line_set:
                        is_hit = True
                        break
                
                if is_hit:
                    nodes.append(node)
                return

            # Recursive step.
            for child in node.children:
                visit(child)
        
        visit(root)
        return nodes

    def analyze_changes(self, old_code: str, new_code: str, old_lines: List[int], new_lines: List[int]) -> List[Dict]:
        """
        Generate raw diff events based on leaf nodes.
        Produces 'added' and 'deleted' events for leaf nodes that strictly
        fall within the target changed lines.
        """
        old_tree = self.parse(old_code)
        self.new_tree = self.parse(new_code)
        new_tree = self.new_tree
        
        new_nodes = self.find_nodes_at_lines(new_tree, new_lines)
        old_nodes = self.find_nodes_at_lines(old_tree, old_lines)
        
        events = []
        
        # --- Leaf matching logic ---
        # Strategy: match nodes by key. Matched nodes are treated as unchanged
        # or handled elsewhere. Unmatched old nodes are "deleted"; unmatched
        # new nodes are "added".
        
        from collections import defaultdict
        old_map = defaultdict(list)
        
        for node in old_nodes:
            key = self._get_leaf_key(node)
            old_map[key].append(node)

        processed_old_ids = set()
        node_mapping = {} # New Node ID -> Old Node Object

        for node in new_nodes:
            key = self._get_leaf_key(node)
            
            # Simple greedy matching: take the first available old node with the same key.
            matched_old = None
            if key in old_map and old_map[key]:
                matched_old = old_map[key].pop(0)
                processed_old_ids.add(matched_old.id)
                node_mapping[node.id] = matched_old
                # Successful match: treat as unchanged/ignored at this level.
                pass
            else:
                events.append({
                    "type": "added",
                    "node_type": node.type,
                    "node_text": node.text.decode('utf8', errors='replace'),
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "node": node
                })

        # Process delete events.
        for key, nodes in old_map.items():
            for node in nodes:
                # Remaining unmatched nodes.
                events.append({
                    "type": "deleted",
                    "node_type": node.type,
                    "node_text": node.text.decode('utf8', errors='replace'),
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "node": node
                })
         
        return events, new_tree, old_tree, node_mapping

    def _get_selector_flow(self, node):
        """
        Capture type flow in qualified chains such as A.B.C.
        - Upstream: node type on the left side of the current node when the current node is a field.
        - Downstream: node type on the right side when the current node or its parent is an operand.
        """
        upstream_type = ""
        downstream_type = ""
        
        parent = node.parent
        if parent and parent.type == 'selector_expression':
            # Case 1: current node is the field on the right side, e.g. B in A.B.
            field = parent.child_by_field_name('field')
            if field and field.id == node.id:
                 # Upstream: operand (A).
                 operand = parent.child_by_field_name('operand')
                 if operand: upstream_type = operand.type
                 
                 # Downstream: check whether parent (A.B) is the grandparent operand.
                 grandparent = parent.parent
                 if grandparent and grandparent.type == 'selector_expression':
                     gp_operand = grandparent.child_by_field_name('operand')
                     if gp_operand and gp_operand.id == parent.id:
                          gp_field = grandparent.child_by_field_name('field')
                          if gp_field: downstream_type = gp_field.type
            
            # Case 2: current node is the operand on the left side, e.g. A in A.B.
            else:
                operand = parent.child_by_field_name('operand')
                if operand and operand.id == node.id:
                     # Upstream: none; it is the root of this link.
                     # Downstream: field (B).
                     field = parent.child_by_field_name('field')
                     if field: downstream_type = field.type

        return upstream_type, downstream_type

    def _get_leaf_key(self, node) -> tuple:
        """
        Leaf fingerprint: (type, text, parent_type, upstream_type, downstream_type).
        This captures structural position inside call chains/selectors.
        """
        text = node.text.decode('utf8', errors='replace')
        
        # Use selector flow to get finer-grained context in A.B.C.
        upstream, downstream = self._get_selector_flow(node)
        
        # Fallback/additional context: keep parent_type for non-selector cases.
        # For example, in 'func(rd)', parent is 'call_expression' and flow is empty,
        # so parent_type provides generic context such as call arguments.
        parent = node.parent
        parent_type = parent.type if parent else ""
        
        # If selector flow is active, it may provide more specific semantics than parent_type.
        # Combine them as (type, text, parent_type, upstream, downstream).
        return (node.type, text, parent_type, upstream, downstream)

    def _get_unique_node_key(self, node):
        return self._get_leaf_key(node)
