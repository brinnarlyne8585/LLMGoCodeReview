from typing import List, Dict, Set
from tree_sitter import Node
from .base_matcher import BaseMatcher

class IdentifierLevelMatcher(BaseMatcher):
    """
    Extract all identifiers from modified lines and execute the selected LSP queries.

    Additional rules:
    - Rule-15: run get_definition on all identifiers.
    - Rule-16: run get_references on all identifiers.
    - Rule-17: run get_definition + get_references on all identifiers.
    """

    def _extract_all_identifiers(self, tree, lines: List[int]) -> List[Dict]:
        """
        Extract all identifier, field_identifier, and type_identifier nodes on the selected lines.
        Returns: [{"node": Node, "line": int, "text": str}, ...]
        """
        if not tree or not lines:
            return []

        identifiers = []
        root = tree.root_node
        line_set = set(lines)
        min_line = min(lines)
        max_line = max(lines)

        def visit(node: Node):
            # Prune nodes outside the target line range.
            if node.end_point[0] < min_line or node.start_point[0] > max_line:
                return

            # Check whether this is an identifier type, including type identifiers.
            if node.type in ('identifier', 'field_identifier', 'type_identifier'):
                n_start = node.start_point[0]
                n_end = node.end_point[0]

                # Check whether the node intersects any target line.
                for l in range(n_start, n_end + 1):
                    if l in line_set:
                        text = node.text.decode('utf8', errors='replace')
                        identifiers.append({
                            "node": node,
                            "line": n_start + 1,  # 1-indexed
                            "text": text
                        })
                        break

            # Recursively traverse child nodes.
            for child in node.children:
                visit(child)

        visit(root)
        return identifiers

    def _should_skip_identifier(self, text: str) -> bool:
        """Return whether this identifier should be skipped, such as built-in types or keywords."""
        return (text in self.GO_BUILTINS or
                text in self.GO_BUILTIN_FUNCS or
                text in self.GO_BUILTIN_VALUES or
                text == '_')

    def _match_rule_15_all_identifiers_definition(self, new_tree, new_lines, add_cmd):
        """
        Rule 15: run get_definition on all identifiers in modified lines.
        """
        identifiers = self._extract_all_identifiers(new_tree, new_lines)

        for item in identifiers:
            text = item['text']
            if self._should_skip_identifier(text):
                continue

            # Get the fully qualified name.
            full_name = self._get_full_qualified_name(item['node'])
            add_cmd('get_definition', str(item['line']), full_name, 'Rule-15')

    def _match_rule_16_all_identifiers_references(self, new_tree, new_lines, add_cmd):
        """
        Rule 16: run get_references on all identifiers in modified lines.
        """
        identifiers = self._extract_all_identifiers(new_tree, new_lines)

        for item in identifiers:
            text = item['text']
            if self._should_skip_identifier(text):
                continue

            full_name = self._get_full_qualified_name(item['node'])
            add_cmd('get_references', str(item['line']), full_name, 'Rule-16')

    def _match_rule_17_all_identifiers_both(self, new_tree, new_lines, add_cmd):
        """
        Rule 17: run get_definition + get_references on all identifiers in modified lines.
        """
        identifiers = self._extract_all_identifiers(new_tree, new_lines)

        for item in identifiers:
            text = item['text']
            if self._should_skip_identifier(text):
                continue

            full_name = self._get_full_qualified_name(item['node'])
            add_cmd('get_definition', str(item['line']), full_name, 'Rule-17')
            add_cmd('get_references', str(item['line']), full_name, 'Rule-17')
