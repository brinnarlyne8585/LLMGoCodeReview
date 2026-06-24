from typing import List, Dict
from tree_sitter import Node
from .base_matcher import BaseMatcher

class VariableLevelMatcher(BaseMatcher):

    def _is_variable_level_definition(self, node):
        """Return whether an identifier is being defined on the LHS rather than used."""
        parent = node.parent
        if not parent: return False
        
        # 1. Variable, constant, field, and parameter declarations.
        if parent.type in ('var_spec', 'const_spec', 'field_declaration', 'parameter_declaration'):
            # The name is usually in the 'name' field.
            if parent.child_by_field_name('name') == node:
                return True
            # var_spec may contain a name list; for now, rely on child_by_field_name('name').
            return False
            
        # 2. Handle expression_list wrappers, such as short_var_declaration and range_clause.
        ancestor = parent
        if parent.type == 'expression_list':
            ancestor = parent.parent
        
        if not ancestor: return False

        # 2.1 Short variable declaration: x, y := ...
        if ancestor.type == 'short_var_declaration':
            left = ancestor.child_by_field_name('left')
            # Check whether node, or its parent expression_list, is the left field.
            if left == node or left == parent:
                return True
                
        # 2.2 Range clause: for i, v := range ...
        if ancestor.type == 'range_clause':
            left = ancestor.child_by_field_name('left')
            if left == node or left == parent:
                return True
        
        return False

    def _is_call_function_name(self, node):
        """Return whether the node is the function name part of a call, handled by Rule 7/8/10."""
        parent = node.parent
        if not parent: return False
        
        # 1. Direct call: func()
        if parent.type == 'call_expression':
            if parent.child_by_field_name('function') == node:
                return True
                
        # 2. Method call: obj.method()
        if parent.type == 'selector_expression':
            grandpa = parent.parent
            if grandpa and grandpa.type == 'call_expression':
                if grandpa.child_by_field_name('function') == parent:
                    # Check whether node is the selector field, i.e. the method name.
                    if parent.child_by_field_name('field') == node:
                        return True
        return False

    def _is_variable_usage(self, node):
        """Return whether the node is a potential variable, field, or constant usage, excluding definitions and function names."""
        
        # 1. Exclude keywords.
        target = node.text.decode('utf8', errors='replace')
        if target in self.GO_BUILTIN_FUNCS or target in self.GO_BUILTIN_VALUES:
            return False

        # 2. Exclude function call names handled by Rule 7/8/10.
        if self._is_call_function_name(node):
            return False

        # 3. Exclude definitions handled by Rule 3/9.
        if self._is_variable_level_definition(node):
            return False

        # 4. Exclude names in function declarations handled by Rule 9.
        if node.parent and node.parent.type in ('function_declaration', 'method_declaration'):
            return False
            
        # 5. Exclude keys in struct initialization, i.e. field names.
        # keyed_element: key: value
        if node.parent and node.parent.type == 'keyed_element':
            if node.parent.child_by_field_name('key') == node:
                return False

        return True

    def _match_rule_11_new_usage(self, change, add_cmd):
        """
        Rule 11:
        If the modification involves using a new parameter/variable/field/property/constant,
        execute get_definition() on that symbol.
        """
        
        if change['type'] != 'added': return

        node = change.get('node')
        if not node or node.type not in ('identifier', 'field_identifier'): return
        
        # Core logic: trigger when this is a variable usage.
        if self._is_variable_usage(node):
            line = str(change['start_line'])
            target = self._get_full_qualified_name(node)
            add_cmd("get_definition", line, target, "Rule-11")

    def _match_rule_12_new_declaration(self, change, add_cmd):
        """
        Rule 12:
        If a new parameter/variable/field/property/constant is declared,
        execute get_references() on that symbol.
        """

        if change['type'] != 'added': return

        node = change.get('node')
        if not node or node.type not in ('identifier', 'field_identifier'): return

        # Core logic: trigger when this is a variable definition.
        # _is_variable_level_definition covers var, const, field, param, short_var (LHS), and range (LHS),
        # while excluding function_declaration handled by Rule 9.
        if self._is_variable_level_definition(node):
            target = self._get_full_qualified_name(node)
            if target == '_': return  # Exclude the Go blank identifier.
            line = str(change['start_line'])
            # Trigger get_references at the definition site.
            add_cmd("get_references", line, target, "Rule-12")

    def _match_rule_13_value_modification(self, change, all_changes, new_tree, new_lines, add_cmd, node_mapping=None):
        """Rule 13: variable, field, or parameter value is modified through assignment or inc/dec."""
        
        node = change.get('node')
        if not node: return

        # Walk upward to find the assignment statement.
        curr = node
        assignment_node = None
        while curr:
            if curr.type in ('assignment_statement', 'inc_statement', 'dec_statement'):
                assignment_node = curr
                break
            # Boundary: no need to walk too far; this is usually within one statement.
            if curr.type in ('function_declaration', 'method_declaration', 'block', 'source_file'):
                break
            curr = curr.parent
            
        if not assignment_node: return
        
        # Extract modified targets from the LHS.
        lhs = None
        if assignment_node.type == 'assignment_statement':
            left = assignment_node.child_by_field_name('left')
            lhs = left
        elif assignment_node.type in ('inc_statement', 'dec_statement'):
            lhs = assignment_node.child_by_field_name('operand')
            
        if not lhs: return

        # Collect all identifiers in the LHS.
        targets = []
        
        def collect_ids(n):
            if not n: return
            if n.type in ('identifier', 'field_identifier'):
                targets.append(n)
            elif n.type in ('expression_list', 'selector_expression', 'index_expression'):
                if n.type == 'selector_expression':
                    field = n.child_by_field_name('field')
                    if field: targets.append(field)
                else:
                    for i in range(n.child_count):
                        collect_ids(n.child(i))
        
        collect_ids(lhs)

        # Case A: Deleted, where an assignment operation was removed.
        # Check whether these targets still exist in the new code as references or reassignment targets.
        if change['type'] == 'deleted':
             if not new_tree: return
             new_lines_set = set(new_lines) if new_lines else set()
             
             for tgt in targets:
                 target_name = self._get_full_qualified_name(tgt)
                 # Search the new code within the hunk for the same symbol name.
                 cursor = new_tree.walk()
                 found_line = None
                 visited_children = False
                 
                 while True:
                     if not visited_children:
                         # Check Identifier / Field Identifier.
                         if cursor.node.type in ('identifier', 'field_identifier'):
                             if cursor.node.text.decode('utf8', errors='replace') == target_name:
                                 # Must be inside the hunk.
                                 ln = cursor.node.start_point[0] + 1
                                 if ln in new_lines_set:
                                     # Further check whether the symbol is in an assignment/modification statement that does not contain deleted_text.
                                     # 1. Walk upward to find an assignment.
                                     new_curr = cursor.node
                                     new_assign = None
                                     while new_curr:
                                         if new_curr.type in ('assignment_statement', 'inc_statement', 'dec_statement'):
                                             new_assign = new_curr
                                             break
                                         if new_curr.type in ('function_declaration', 'method_declaration', 'block', 'source_file'):
                                             break
                                         new_curr = new_curr.parent
                                     
                                     if new_assign:
                                          # 2. Check that deleted_text is not present in the new assignment statement (Greedy Absence Check).
                                          # A simple substring check may be imprecise, but is usually enough for deleted token text.
                                          # Here node is the deleted node.
                                          deleted_text = node.text.decode('utf8', errors='replace').strip()
                                          
                                          # Check all child-node text under the assignment.
                                          is_present = False
                                          # A simple text containment check could inspect whether the assignment text contains deleted_text.
                                          # Traversing child tokens is still cheap and helps avoid partial matches such as deleted 'val' matching 'value'.
                                          assign_cursor = new_assign.walk()
                                          ac_visited = False
                                          while True:
                                              if not ac_visited:
                                                  if assign_cursor.node.child_count == 0:
                                                       if assign_cursor.node.text.decode('utf8', errors='replace').strip() == deleted_text:
                                                           is_present = True
                                                           break
                                                  if assign_cursor.goto_first_child(): continue
                                              
                                              if assign_cursor.goto_next_sibling(): ac_visited = False
                                              elif assign_cursor.goto_parent(): ac_visited = True
                                              else: break
                                          
                                          if not is_present:
                                              found_line = str(ln)
                                              break
                         
                         if cursor.goto_first_child():
                             continue

                     if cursor.goto_next_sibling():
                         visited_children = False
                     elif cursor.goto_parent():
                         visited_children = True
                     else:
                         break
                
                 if found_line:
                     # Treat this deleted assignment as a modification event only if the variable still appears in the new code.
                     add_cmd("get_definition", found_line, target_name, "Rule-13")
                     add_cmd("get_references", found_line, target_name, "Rule-13")
             return

        # Case B: Added / Modified, i.e. a new assignment.
        # Check whether targets are existing nodes rather than newly added nodes.
        # Use a unique key: (start_byte, end_byte, type).
        added_node_keys = {
            (c['node'].start_byte, c['node'].end_byte, c['node'].type) 
            for c in all_changes if c['type'] == 'added'
        }
        
        for tgt in targets:
            tgt_key = (tgt.start_byte, tgt.end_byte, tgt.type)
            
            # Core filter: the node must not be newly added.
            if tgt_key in added_node_keys:
                continue

            # 3. Tree-Based Consistency Check, comparing old and new trees.
            # Compare RHS directly from the old and new trees instead of relying on diff results.
            
            is_consistent_move = False
            comparison_done = False
            
            # Try to find the corresponding old LHS node through node_mapping.
            old_tgt = None
            if node_mapping and tgt.id in node_mapping:
                old_tgt = node_mapping[tgt.id]
            
            if old_tgt:
                # Walk upward to find the old assignment.
                old_curr = old_tgt
                old_assignment = None
                while old_curr:
                    if old_curr.type in ('assignment_statement', 'inc_statement', 'dec_statement'):
                        old_assignment = old_curr
                        break
                    if old_curr.type in ('function_declaration', 'method_declaration', 'block', 'source_file'):
                        break
                    old_curr = old_curr.parent
                
                if old_assignment:
                    # Found the old assignment. Start comparing RHS.
                    old_rhs_text = ""
                    new_rhs_text = ""
                    
                    if old_assignment.type == 'assignment_statement':
                        old_right = old_assignment.child_by_field_name('right')
                        if old_right: old_rhs_text = old_right.text.decode('utf8', errors='replace')
                    elif old_assignment.type in ('inc_statement', 'dec_statement'):
                        # Inc/Dec means Value Change (+1)
                        # What if the new node is also inc/dec with identical text?
                        pass 
                    
                    if assignment_node.type == 'assignment_statement':
                        new_right = assignment_node.child_by_field_name('right')
                        if new_right: new_rhs_text = new_right.text.decode('utf8', errors='replace')
                    
                    # Simple text comparison after removing all whitespace to avoid formatting differences.
                    def clean_text(s):
                        return "".join(s.split())
                    
                    if clean_text(old_rhs_text) == clean_text(new_rhs_text):
                        is_consistent_move = True
                        comparison_done = True
                    else:
                        # Different text confirms this as a modification.
                        is_consistent_move = False
                        comparison_done = True
            
            # Decision logic:
            if comparison_done:
                if is_consistent_move:
                    continue # Consistent -> Code Move -> Skip

            # Trigger Rule 13.
            line = str(tgt.start_point[0] + 1)
            name = self._get_full_qualified_name(tgt)
            
            # Exclude _ (blank identifier).
            if name == '_': continue
            
            # Restrict triggering to targets whose LHS line is actually within the current hunk.
            if new_lines and int(line) not in new_lines:
                continue
            
            add_cmd("get_definition", line, name, "Rule-13")
            add_cmd("get_references", line, name, "Rule-13")

    def _match_rule_14_hardcoded_assignment(self, change, all_changes, add_cmd):
        """Rule 14: If a hardcoded value is assigned, execute get_search()."""
        # Only added string literal nodes are relevant.
        if change['type'] != 'added': return
        node = change.get('node')
        if not node: return
        
        # Go string literals: interpreted_string_literal ("...")
        if node.type not in ('interpreted_string_literal'):
            return

        # Check context: whether the node is on the RHS of an assignment.
        # Walk upward to see whether it is contained in the RHS of Assignment / Var Decl / Const Decl.
        
        curr = node
        is_rhs = False
        assignment_found = False
        
        while curr:
            parent = curr.parent
            if not parent: break
            
            # Case 1: Assignment Statement (LHS = RHS)
            if parent.type == 'assignment_statement':
                # Check if current node is in the 'right' field list
                # assignment_statement has field 'right' which is a list (expression_list) -> or just child?
                # Tree-sitter go: assignment_statement -> left: expression_list, right: expression_list
                # So we check if our ancestor is the 'right' child of assignment
                rights = parent.child_by_field_name('right') # This gets the expression_list
                if rights and self._is_descendant(curr, rights):
                    is_rhs = True
                    assignment_found = True
                    break
            
            # Case 2: Short Var Declaration (LHS := RHS)
            elif parent.type == 'short_var_declaration':
                rights = parent.child_by_field_name('right')
                if rights and self._is_descendant(curr, rights):
                    is_rhs = True
                    assignment_found = True
                    break
            
            # Case 3: Var Spec / Const Spec (var X = RHS, const X = RHS)
            elif parent.type in ('var_spec', 'const_spec', 'type_alias', 'field_declaration'): 
                 # In tree-sitter-go, the const_spec field may be 'value' or 'values', depending on grammar version/details.
                 # Usually it is 'value' -> expression_list. Try both.
                 values = parent.child_by_field_name('value')
                 if not values: values = parent.child_by_field_name('values')
                 
                 if values and self._is_descendant(curr, values):
                     is_rhs = True
                     assignment_found = True
                     break
            
            # Case 4: Keyed Element (Struct literal / Map literal value)
            # e.g. Data: "hardcoded"
            elif parent.type == 'keyed_element':
                value = parent.child_by_field_name('value')
                if value and self._is_descendant(curr, value):
                    is_rhs = True
                    assignment_found = True
                    break
            
            # Stop if we hit function/block boundaries generally
            if parent.type in ('function_declaration', 'method_declaration', 'source_file'):
                break
                
            curr = parent
            
        if assignment_found and is_rhs:
            # Found a hardcoded string assignment.
            line = str(node.start_point[0] + 1)
            value_text = node.text.decode('utf8', errors='replace')

            add_cmd("get_search", line, value_text, "Rule-14")

    def _is_descendant(self, node, ancestor):
        """Helper to check if node is descendant of (or equal to) ancestor"""
        if node.id == ancestor.id: return True
        curr = node
        while curr:
            if curr.id == ancestor.id: return True
            if curr.parent is None: break # Optimization
            if curr.start_byte < ancestor.start_byte or curr.end_byte > ancestor.end_byte:
                # Scoping optimization: node cannot be child if outside bounds
                return False 
            curr = curr.parent
        return False
