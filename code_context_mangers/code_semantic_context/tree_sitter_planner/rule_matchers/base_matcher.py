from typing import List, Dict, Set

class BaseMatcher:
    
    GO_BUILTINS = {
        'bool', 'byte', 'complex64', 'complex128', 'error', 'float32', 'float64',
        'int', 'int8', 'int16', 'int32', 'int64', 'rune', 'string',
        'uint', 'uint8', 'uint16', 'uint32', 'uint64', 'uintptr', 'any', 'comparable',
        'true', 'false', 'iota', 'nil'
    }

    GO_BUILTIN_FUNCS = {
        'len', 'cap', 'new', 'make', 'append', 'copy', 'close', 'delete',
        'complex', 'real', 'imag', 'panic', 'recover', 'print', 'println',
        'clear', 'min', 'max',
    }

    GO_BUILTIN_VALUES = {
        'true', 'false', 'nil', 'iota', '_',
    }

    def __init__(self):
        pass

    def evaluate(self, changes: List[Dict], new_tree=None, new_lines=None) -> List[Dict]:
        """
        Base evaluate method to be overridden by subclasses.
        Accepts changes, optional new AST tree, and optional active valid lines (new_lines).
        """
        return []

    # --- Helpers ---

    def _create_cmd(self, cmd, line, target, rule_id):
        return {
            "command": cmd,
            "line": line,
            "target_symbol": target,
            "index": 0, 
            "source_rule_id": rule_id
        }

    def _extract_name_from_node(self, node):
        name_node = node.child_by_field_name('name')
        if name_node: return name_node.text.decode('utf8', errors='replace')
        return ""

    def _get_full_qualified_name(self, node):
        """
        Recursively resolve a fully qualified name, such as 'time.Duration' or 'A.B.C'.
        Continue upward when the current node is the 'field' or 'name' part of a
        selector/qualified identifier. Stop when the node is the left-side
        'operand' or 'package' part.
        """
        curr = node
        while curr.parent:
            parent = curr.parent
            should_ascend = False
            
            # Case 1: selector_expression, e.g. A.B, where field is B.
            if parent.type == 'selector_expression':
                field = parent.child_by_field_name('field')
                if field and field.id == curr.id:
                    should_ascend = True
            
            # Case 2: qualified_type, e.g. time.Duration, where name is Duration.
            elif parent.type == 'qualified_type':
                name = parent.child_by_field_name('name')
                if name and name.id == curr.id:
                    should_ascend = True
            
            if should_ascend:
                curr = parent
            else:
                break
                
        return curr.text.decode('utf8', errors='replace')
