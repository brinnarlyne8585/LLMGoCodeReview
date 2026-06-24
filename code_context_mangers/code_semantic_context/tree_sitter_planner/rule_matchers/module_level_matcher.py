from typing import List, Dict
from tree_sitter import Node
from .base_matcher import BaseMatcher

class ModuleLevelMatcher(BaseMatcher):


    def _match_rule_1_import(self, change, add_cmd):
        """
        Rule-1: If a new package/library/module is imported, execute get_references() on it.
        """
        if change['type'] != 'added': return

        node = change.get('node')
        if not node: return

        # Backtrack to check whether the node is inside import_spec.
        curr = node
        while curr:
            if curr.type == 'import_spec':
                # Matched.
                line = str(change['start_line'])
                # The import path is usually a string_literal.
                path_node = curr.child_by_field_name('path')
                if path_node:
                    target = path_node.text.decode('utf8', errors='replace').strip('"')
                    add_cmd("get_references", line, target, "Rule-1")
                return
            if curr.type == 'source_file': break
            curr = curr.parent

    def _match_rule_2_new_type(self, change, add_cmd):
        """
        Rule 2: If a new type is introduced in a declaration, execute get_definition() on that type.
        """

        # Only newly added type usages are relevant.
        if change['type'] != 'added': return

        node = change.get('node')
        if not node: return

        # In Go tree-sitter, type usages are usually represented as 'type_identifier'.
        # Examples include field types, return types, and composite literal types.
        if node.type == 'type_identifier':
            # Filter out the type definition itself.
            # Example: type MyStruct struct { ... } -> 'MyStruct' is the type_identifier in type_spec.name.

            # Check whether the parent node is the definition name.
            if node.parent and node.parent.type == 'type_spec':
                name_node = node.parent.child_by_field_name('name')
                if name_node and name_node.id == node.id:
                    # This is a type definition, not a reference to another type.
                    return

            # A 'type_identifier' that is not a definition is likely the reference we want.

            # Skip Go built-in types to avoid invalid get_definition calls.
            target = self._get_full_qualified_name(node)
            if target in self.GO_BUILTINS:
                return

            line = str(change['start_line'])
            add_cmd("get_definition", line, target, "Rule-2")

    def _match_rule_3_new_interface(self, change, add_cmd):
        """
        Rule 3: If a new class/interface is added, execute get_references() on that type.
        """

        # Only added nodes are relevant.
        if change['type'] != 'added': return

        node = change.get('node')
        if not node: return

        # Core logic: find the 'name' node of the type definition.
        # type ListContainersResponse struct { ... }
        # Node: ListContainersResponse (type_identifier)
        # Parent: type_spec
        # Field: name

        if node.type == 'type_identifier':
            if node.parent and node.parent.type == 'type_spec':
                name_node = node.parent.child_by_field_name('name')
                if name_node and name_node.id == node.id:
                    # This is a new type definition, or the new definition after a rename.
                    # Structs, interfaces, and aliases such as type Alias int are all treated as new type definitions.
                    # Rule 3 requires reference lookup to find who uses this new type.
                    target = node.text.decode('utf8', errors='replace')
                    line = str(change['start_line'])
                    add_cmd("get_references", line, target, "Rule-3")

    def _match_rule_5_emulated(self, change, changes, add_cmd):
        """
        Rule 5: If extends/implements relationships are changed,
        execute get_definition() on the changed supertype names
        and execute get_references() on the current type name.
        """
        # Added, modified, and deleted changes are relevant.
        if change['type'] not in ('added', 'modified', 'deleted'): return

        node = change.get('node')
        if not node: return

        # Filter out obviously irrelevant nodes early, such as comments, punctuation, and keywords.
        if not node.is_named or node.type == 'comment': return

        # 1. Backtrack to find type_spec while checking whether the path is interrupted by a non-structural node.
        # We are looking for structural changes in the type definition.
        type_spec = None
        curr = node
        is_non_structural_context = False

        while curr:
            if curr.type == 'field_declaration':
                # If the node is inside a field_declaration that has a name,
                # it is part of a property, e.g. PullInterval time.Duration.
                if curr.child_by_field_name('name'):
                    is_non_structural_context = True
                    break

            if curr.type in ('method_spec', 'method_elem'):
                # method_spec: method declaration, usually for methods associated with a struct, even though Go methods are top-level.
                # method_elem: method element inside an interface.
                # Nodes inside these contexts indicate method signature changes, handled by Rule 10 rather than Rule 5.
                is_non_structural_context = True
                break

            if curr.type == 'type_spec':
                type_spec = curr
                break
            if curr.type == 'source_file': break
            curr = curr.parent

        if is_non_structural_context:
            return

        if not type_spec: return

        # 2. Check whether this is the type name itself, or whether the type spec is newly defined.

        name_node = type_spec.child_by_field_name('name')
        if name_node and name_node.id == node.id:
            return

        # Check if type_spec is Newly Defined using 'changes' context
        if name_node:
            for c in changes:
                if c['type'] == 'added':
                    c_node = c.get('node')
                    # If an added change has the same node id as type_spec.name_node.id,
                    # this type is newly defined, so skip Rule 5.
                    if c_node and c_node.id == name_node.id:
                        return

        # 3. Action 1: look up references to the current type because its structure changed.
        if name_node:
            target_ref = name_node.text.decode('utf8', errors='replace')
            # Use the type_spec line number for better accuracy.
            ref_line = str(type_spec.start_point[0] + 1)
            add_cmd("get_references", ref_line, target_ref, "Rule-5")

        # 4. Action 2: look up definitions for newly introduced types, such as supertypes or key types.
        if change['type'] == 'added' and node.type == 'type_identifier':
            target_def = node.text.decode('utf8', errors='replace')

            # Skip built-in types.
            if target_def not in self.GO_BUILTINS:
                # Definition lookup can use the changed node line for easier localization.
                def_line = str(change['start_line'])
                add_cmd("get_definition", def_line, target_def, "Rule-5")

    def _match_rule_6_rename(self, change, add_cmd):
        """
        Rule 6: If a type is renamed (or deleted), execute get_search() on the old name.
        """

        # Only deleted nodes are relevant.
        if change['type'] != 'deleted': return

        node = change.get('node')
        if not node: return

        # Symmetric with Rule 3: find cases where the name node of type_spec was deleted.
        if node.type == 'type_identifier':
            if node.parent and node.parent.type == 'type_spec':
                name_node = node.parent.child_by_field_name('name')
                # Confirm that the deleted node is the name of this type_spec.
                if name_node and name_node.id == node.id:
                    target = node.text.decode('utf8', errors='replace')
                    line = str(change['start_line'])
                    add_cmd("get_search", line, target, "Rule-6")
