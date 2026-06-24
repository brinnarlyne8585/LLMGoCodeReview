from typing import List, Dict, Optional

# Import sub-matchers
from .rule_matchers.module_level_matcher import ModuleLevelMatcher
from .rule_matchers.method_level_matcher import MethodLevelMatcher
from .rule_matchers.variable_level_matcher import VariableLevelMatcher
from .rule_matchers.identifier_level_matcher import IdentifierLevelMatcher

class RuleEngine(ModuleLevelMatcher, MethodLevelMatcher, VariableLevelMatcher, IdentifierLevelMatcher):
    def __init__(self, identifier_mode: Optional[str] = None):
        """
        Args:
            identifier_mode: controls identifier-level rule mode
                - None: disabled by default, preserving existing behavior
                - "definition": run get_definition only (Rule-15)
                - "reference": run get_references only (Rule-16)
                - "both": run get_definition + get_references (Rule-17)
        """
        super().__init__()
        self.identifier_mode = identifier_mode

    def evaluate(self, changes: List[Dict], new_tree=None, new_lines=None, old_tree=None, node_mapping=None) -> List[Dict]:
        """
        Main entry point. Selects one mutually exclusive rule set according to identifier_mode.

        Four modes:
        - None: run the 14 semantic rules (Rule 1-14)
        - "definition": run get_definition for all identifiers (Rule-15)
        - "reference": run get_references for all identifiers (Rule-16)
        - "both": run get_definition + get_references for all identifiers (Rule-17)
        """
        commands = []
        seen_symbols = set()

        def add_cmd(cmd, line, target, rule_id):
            symbol_key = (rule_id, target, cmd)
            if symbol_key not in seen_symbols:
                seen_symbols.add(symbol_key)
                commands.append(self._create_cmd(cmd, line, target, rule_id))

        # --- Select which rule set to execute according to mode ---
        if self.identifier_mode:
            # Modes 2/3/4: identifier-level rules, replacing the 14 semantic rules.
            if not new_tree or not new_lines:
                return []

            if self.identifier_mode == "definition":
                self._match_rule_15_all_identifiers_definition(new_tree, new_lines, add_cmd)
            elif self.identifier_mode == "reference":
                self._match_rule_16_all_identifiers_references(new_tree, new_lines, add_cmd)
            elif self.identifier_mode == "both":
                self._match_rule_17_all_identifiers_both(new_tree, new_lines, add_cmd)
        else:
            # Mode 1: run the 14 semantic rules.
            for change in changes:
                # --- Module-level rules ---
                # Rule 1: If a new package/library/module is imported, execute get_references() on it.
                self._match_rule_1_import(change, add_cmd)

                # Rule 2: If a new type reference is introduced, execute get_definition() on that type.
                self._match_rule_2_new_type(change, add_cmd)

                # Rule 3: If a new class/interface is added, execute get_references() on that type.
                self._match_rule_3_new_interface(change, add_cmd)

                # Rule 5: If extends/implements relationships are changed, execute get_definition() on the changed supertype names and execute get_references() on the current type name.
                self._match_rule_5_emulated(change, changes, add_cmd)

                # Rule 6: If a type is renamed (or deleted), execute get_search() on the old name.
                self._match_rule_6_rename(change, add_cmd)

                # --- Method-level rules ---
                # Rule 7: If a new function call is added, execute get_definition() on that function.
                self._match_rule_7_new_call(change, add_cmd)

                # Rule 8: If the argument of a function call is modified (in any way), execute get_definition() on that function.
                self._match_rule_8_args_changed(change, changes, new_tree, new_lines, add_cmd)

                # Rule 9: If a new function is added, execute get_references() on that function.
                self._match_rule_9_new_func(change, add_cmd)

               # Rule 10: If the signature, return behavior, or control flow of a function is modified (in any way), execute get_references() on that function.
                self._match_rule_10_func_sig_changed(change, changes, new_tree, new_lines, add_cmd)

                # --- Variable-level rules ---
                # Rule 11: If the modification involves using a new parameter/variable/field/property/constant, execute get_definition() on that symbol.
                self._match_rule_11_new_usage(change, add_cmd)

                # Rule 12: If a new parameter/variable/field/property/constant is declared, execute get_references() on that symbol.
                self._match_rule_12_new_declaration(change, add_cmd)

                # Rule 13: If variable value is modified, execute get_definition() and get_references().
                self._match_rule_13_value_modification(change, changes, new_tree, new_lines, add_cmd, node_mapping)

                # Rule 14: If a hardcoded value is assigned, execute get_search().
                self._match_rule_14_hardcoded_assignment(change, changes, add_cmd)

        # Sort by line number.
        commands.sort(key=lambda x: int(x['line']) if x['line'].isdigit() else 0)
        
        # Re-index.
        for i, cmd in enumerate(commands):
            cmd['index'] = i + 1
            
        return commands
