from typing import List, Dict
from .base_matcher import BaseMatcher

class MethodLevelMatcher(BaseMatcher):

    def _match_rule_7_new_call(self, change, add_cmd):
        """
        Rule 7: If a new function call is added, execute get_definition() on that function.
        """
        # Only added nodes are relevant.
        if change['type'] != 'added': return

        node = change.get('node')
        if not node: return

        target_func = None

        # Case A: direct call -> Func(args)
        # Node: 'Func' (identifier)
        # Parent: call_expression
        # Field: function
        if node.type == 'identifier':
            parent = node.parent
            if parent and parent.type == 'call_expression':
                func_node = parent.child_by_field_name('function')
                if func_node and func_node.id == node.id:
                    target_func = node.text.decode('utf8', errors='replace')

        # Case B: method call -> obj.Method(args)
        # Or package call -> pkg.Func(args).
        # Node: 'Method' (field_identifier)
        # Parent: selector_expression
        # Selector Parent: call_expression
        elif node.type == 'field_identifier':
            parent = node.parent
            if parent and parent.type == 'selector_expression':
                # Confirm the selector is the function part of the call_expression.
                grandpa = parent.parent
                if grandpa and grandpa.type == 'call_expression':
                    func_node = grandpa.child_by_field_name('function')
                    # func_node should be parent (selector_expression).
                    if func_node and func_node.id == parent.id:
                        target_func = node.text.decode('utf8', errors='replace')

        if target_func:
            # Skip built-in functions.
            if target_func in self.GO_BUILTIN_FUNCS:
                return

            # Rule 7 requires a definition lookup.
            line = str(change['start_line'])
            add_cmd("get_definition", line, target_func, "Rule-7")

    def _match_rule_8_args_changed(self, change, changes, new_tree, new_lines, add_cmd):
        """Rule 8: If function call arguments change in any way, run get_definition() on that function."""
        # Added, modified, and deleted changes are relevant.
        # For deleted nodes, use new_tree and new_lines (hunk context) to recover the location in the new file.
        if change['type'] not in ('added', 'modified', 'deleted'): return

        node = change.get('node')
        if not node: return

        # Filter out irrelevant nodes.
        if not node.is_named or node.type == 'comment': return

        # Backtrack to find the argument_list.
        curr = node
        call_expr = None

        while curr:
            if curr.type == 'argument_list':
                call_expr = curr.parent
                break
            if curr.type in ('function_declaration', 'method_declaration', 'func_literal', 'type_spec', 'source_file'):
                break
            curr = curr.parent

        if not call_expr or call_expr.type != 'call_expression':
            return

        # Find the callee function name node.
        target_name_node = None
        func_node = call_expr.child_by_field_name('function')
        if not func_node: return

        if func_node.type == 'identifier':
            target_name_node = func_node
        elif func_node.type == 'selector_expression':
            target_name_node = func_node.child_by_field_name('field')

        if not target_name_node: return

        target_func = target_name_node.text.decode('utf8', errors='replace')

        # Check: Is it a New Call (Rule 7)?
        if change['type'] != 'deleted':
            is_new_call = False
            for c in changes:
                if c['type'] == 'added':
                    c_node = c.get('node')
                    if c_node and c_node.id == target_name_node.id:
                        is_new_call = True
                        break
            if is_new_call: return

        # Action: Determine Line Number
        final_line = None

        if change['type'] in ('added', 'modified'):
            final_line = str(call_expr.start_point[0] + 1)

        elif change['type'] == 'deleted':
            # The corresponding function call must be found in new_tree.
            if not new_tree or not new_lines: return

            new_lines_set = set(new_lines) if new_lines else set()
            deleted_text = node.text.decode('utf8', errors='replace').strip()

            cursor = new_tree.walk()
            visited_children = False
            while True:
                if not visited_children:
                    if cursor.node.type == 'call_expression':
                        line_num = cursor.node.start_point[0] + 1
                        if line_num in new_lines_set:
                            # 1. Check Function Name
                            fn = cursor.node.child_by_field_name('function')
                            matched_name = False
                            if fn:
                                name = None
                                if fn.type == 'identifier':
                                    name = fn.text.decode('utf8', errors='replace')
                                elif fn.type == 'selector_expression':
                                    f = fn.child_by_field_name('field')
                                    if f: name = f.text.decode('utf8', errors='replace')
                                if name == target_func:
                                    matched_name = True

                            if matched_name:
                                # 2. Greedy Absence Check: the argument list must not contain deleted_text.
                                new_args_node = cursor.node.child_by_field_name('arguments')
                                is_present = False
                                if new_args_node:
                                    for child in new_args_node.named_children:
                                        if child.text.decode('utf8', errors='replace').strip() == deleted_text:
                                            is_present = True
                                            break
                                if not is_present:
                                    final_line = str(line_num)
                                    break

                    if cursor.goto_first_child(): continue

                if cursor.goto_next_sibling():
                    visited_children = False
                elif cursor.goto_parent():
                    visited_children = True
                else:
                    break

        if not final_line: return

        if target_func in self.GO_BUILTIN_FUNCS:
            return

        add_cmd("get_definition", final_line, target_func, "Rule-8")

    def _match_rule_9_new_func(self, change, add_cmd):
        """
        Rule 9: If a new function is added, execute get_references() on that function.
        """

        # Focus on Added
        if change['type'] != 'added': return

        node = change.get('node')
        if not node: return

        # 1. Backtrack to find function_declaration or method_declaration
        func_decl = None
        curr = node
        while curr:
            if curr.type in ('function_declaration', 'method_declaration'):
                func_decl = curr
                break
            curr = curr.parent

        if not func_decl:
            return

        # 2. Extract Name Node
        name_node = func_decl.child_by_field_name('name')
        if not name_node: return

        # 3. Check whether the changed node means the function is newly added.
        # Case A: the changed node is the function name itself, e.g. only the name changed or a fine-grained change was reported.
        # Case B: the changed node is the whole function declaration, e.g. a full function body was pasted.
        is_new_func = False
        if node.id == name_node.id:
            is_new_func = True
        elif node.id == func_decl.id:
            is_new_func = True

        if not is_new_func: return

        target = name_node.text.decode('utf8', errors='replace')
        if target in self.GO_BUILTIN_FUNCS: return
        line = str(change['start_line'])
        add_cmd("get_references", line, target, "Rule-9")

    def _match_rule_10_func_sig_changed(self, change, changes, new_tree, new_lines, add_cmd):
        """
        Rule 10:
        If the signature, return behavior, or control flow of a function is modified (in any way),
        execute get_references() on that function.
        """
        node = change.get('node')
        if not node: return

        # Find the enclosing function declaration and confirm signature context.
        func_decl = None
        is_signature_context = False
        curr = node
        
        while curr:
            # Check whether the node belongs to the signature.
            if curr.type in ('parameter_list', 'result_parameters', 'method_spec', 'parameter_declaration', 'variadic_parameter_declaration'):
                is_signature_context = True
            
            if curr.type in ('function_declaration', 'method_declaration', 'func_literal'):
                func_decl = curr
                break
            
            if curr.type == 'source_file':
                break
            curr = curr.parent

        # Ignore nodes outside a function or outside signature context.
        if not func_decl or not is_signature_context: 
            return
            
        # Ignore anonymous functions (func_literal)
        if func_decl.type == 'func_literal':
            return

        target_name = self._extract_name_from_node(func_decl)
        if not target_name: return

        # 2. Branch handling: deleted vs. added/modified.
        
        # --- Case A: Deleted ---
        # Here func_decl and node come from the old tree. Check whether the corresponding function exists in the new tree.
        if change['type'] == 'deleted':
            if not new_tree: return
            
            new_lines_set = set(new_lines) if new_lines else set()
            deleted_text = node.text.decode('utf8', errors='replace').strip()
            
            found_line = None
            
            cursor = new_tree.walk()
            visited_children = False
            while True:
                if not visited_children:
                    if cursor.node.type in ('function_declaration', 'method_declaration'):
                        # 1. Hunk range restriction: search strictly within changed hunk lines.
                        line_num = cursor.node.start_point[0] + 1
                        if line_num in new_lines_set:
                             # 2. Check the name.
                            fn_name = self._extract_name_from_node(cursor.node)
                            if fn_name == target_name:
                                # 3. Greedy Absence Check.
                                is_present = False
                                for child in cursor.node.children:
                                    if child.type in ('parameter_list', 'result_parameters'):
                                        tc = child.walk()
                                        vc = False
                                        while True:
                                            if not vc:
                                                if tc.node.text.decode('utf8', errors='replace').strip() == deleted_text:
                                                    is_present = True
                                                    break
                                                if tc.goto_first_child(): continue
                                            if tc.goto_next_sibling(): vc = False
                                            elif tc.goto_parent(): vc = True
                                            else: break
                                        if is_present: break
                                        
                                if not is_present:
                                    found_line = str(line_num)
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
                add_cmd("get_references", found_line, target_name, "Rule-10")
            return

        # --- Case B: Added / Modified ---
        # Here func_decl and node come from the new tree.
        else:
            # Exclude newly added functions; Rule 9 handles them.
            is_new_func = False
            name_node = func_decl.child_by_field_name('name')
            if name_node:
                for c in changes:
                    if c['type'] == 'added':
                        c_node = c.get('node')
                        if c_node and (c_node.id == name_node.id or c_node.id == func_decl.id):
                            is_new_func = True
                            break
            
            if is_new_func:
                return

            line = str(change['start_line'])
            add_cmd("get_references", line, target_name, "Rule-10")
