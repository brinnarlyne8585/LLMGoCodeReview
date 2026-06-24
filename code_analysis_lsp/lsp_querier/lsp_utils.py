from typing import List, Dict, Optional, Tuple, Union, Any


def flatten_symbol_tree(symbols: object,
                        prefer_selection_range: bool = False
                        ) -> List[Dict[str, Union[int, str]]]:
    """Flatten the symbol tree returned by LSP. Supports both
    `{range}` and `{location: {range}}` structures.
    Returns: [{'name': str, 'kind': int, 'start_line': int, 'end_line': int}, ...]
    """
    flat_list: List[Dict[str, Union[int, str]]] = []

    def _pick_range(symbol: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
            Select the range used for line-number calculation:
              - If prefer_selection_range is enabled and the top level contains both
                selectionRange and range, return
                {start: selectionRange.start, end: range.end}
              - Otherwise use the top-level range
              - Otherwise fall back to location.range
            Note: SymbolInformation (with location) has no selectionRange;
            only DocumentSymbol (without location) provides selectionRange.
            """
        # Only try to combine selectionRange + range at the top level
        # for the DocumentSymbol case.
        if prefer_selection_range:
            sel = symbol.get("selectionRange")
            rng_top = symbol.get("range")
            if isinstance(sel, dict) and isinstance(rng_top, dict):
                sel_start = sel.get("start")
                rng_end = rng_top.get("end")
                if isinstance(sel_start, dict) and isinstance(rng_end, dict):
                    return {"start": sel_start, "end": rng_end}

        # Standard fallback: top-level range (DocumentSymbol or server-specific extension).
        rng_top = symbol.get("range")
        if isinstance(rng_top, dict):
            return rng_top

        # Final fallback: location.range (SymbolInformation).
        loc_rng = symbol.get("location", {}).get("range")
        if isinstance(loc_rng, dict):
            return loc_rng

    def _walk(symbol_list: object) -> None:
        if not isinstance(symbol_list, list):
            return
        for symbol in symbol_list:
            if not isinstance(symbol, dict):
                continue
            kind = symbol.get("kind")
            name = symbol.get("name")
            s_range = _pick_range(symbol)
            if kind and name and s_range:
                try:
                    start_line = int(s_range["start"]["line"]) + 1
                    end_line = int(s_range["end"]["line"]) + 1
                    flat_list.append(
                        {"name": name, "kind": kind, "start_line": start_line, "end_line": end_line}
                    )
                except Exception:
                    pass
            if isinstance(symbol.get("children"), list):
                _walk(symbol["children"])

    _walk(symbols)
    return flat_list


def format_type_definition_resp(symbols):

    def _as_locs(r):
        if not r:
            return []
        if isinstance(r, dict) and "uri" in r and "range" in r:  # Location
            return [r]
        if isinstance(r, list):
            if r and isinstance(r[0], dict) and "targetUri" in r[0]:  # LocationLink[]
                return [{"uri": x["targetUri"], "range": x["targetSelectionRange"]} for x in r]
            if r and isinstance(r[0], dict) and "uri" in r[0]:  # Location[]
                return r
        return []

    locs = _as_locs(symbols)
    return locs
