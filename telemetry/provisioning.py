"""Pure helpers for the provisioning script: paging and activation comparison."""

from __future__ import annotations

from collections.abc import Callable

# Fields that define an ABP activation. The frame counters are left out on purpose: they
# move on their own as uplinks arrive and must not trigger a new activation.
ACTIVATION_FIELDS = (
    "dev_addr",
    "app_s_key",
    "nwk_s_enc_key",
    "s_nwk_s_int_key",
    "f_nwk_s_int_key",
)


def activation_matches(current: dict | None, wanted: dict) -> bool:
    """True when the device already has the wanted ABP session (case insensitive hex)."""
    if current is None:
        return False
    return all(str(current.get(f, "")).lower() == str(wanted[f]).lower() for f in ACTIVATION_FIELDS)


def list_all[T](fetch: Callable[[int, int], tuple[list[T], int]], page_size: int = 100) -> list[T]:
    """Collect every item of an offset paged list call.

    ``fetch(limit, offset)`` returns ``(items, total_count)``. Stops at ``total_count`` or at
    the first empty page, whichever comes first.
    """
    if page_size < 1:
        raise ValueError("page_size must be positive")
    out: list[T] = []
    while True:
        items, total = fetch(page_size, len(out))
        out.extend(items)
        if not items or len(out) >= total:
            return out
