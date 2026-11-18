"""Pairing rules for waiting users (D-033)."""

from backend.modules.matching.matcher import pair_users, vip_first


def recent(*pairs: tuple[int, int]) -> set[frozenset[int]]:
    return {frozenset(pair) for pair in pairs}


def test_pairs_in_waiting_order() -> None:
    assert pair_users([1, 2, 3, 4, 5], set()) == [(1, 2), (3, 4)]


def test_nobody_or_one_user_makes_no_pair() -> None:
    assert pair_users([], set()) == []
    assert pair_users([7], set()) == []


def test_recent_partners_are_skipped_while_someone_else_waits() -> None:
    assert pair_users([1, 2, 3], recent((1, 2))) == [(1, 3)]


def test_recent_partners_are_matched_when_nobody_else_is_available() -> None:
    assert pair_users([1, 2], recent((1, 2))) == [(1, 2)]


def test_a_recent_pair_does_not_take_a_fresh_partner_from_someone_else() -> None:
    # 1 and 2 chatted recently; 3 is free. 1 pairs with 3, and 2 is left waiting
    # rather than 2 and 1 being matched again.
    assert pair_users([1, 2, 3], recent((1, 2), (2, 3))) == [(1, 3)]
    # With a fourth user, everyone gets a fresh partner.
    assert pair_users([1, 2, 3, 4], recent((1, 2))) == [(1, 3), (2, 4)]


def test_leftover_recent_pairs_are_matched_when_unavoidable() -> None:
    # 2-3 is the only fresh pair; 1-4 is a repeat no arrangement avoids. Pairs are
    # returned in waiting order.
    assert pair_users([1, 2, 3, 4], recent((1, 2), (1, 3), (1, 4), (3, 4))) == [(1, 4), (2, 3)]


def test_partners_are_swapped_to_avoid_a_forced_repeat() -> None:
    # Greedy pairing gives 1-2, then forces 3-4 (recent); 1-3 and 2-4 avoid it.
    assert pair_users([1, 2, 3, 4], recent((3, 4))) == [(1, 3), (2, 4)]


def test_swaps_never_add_a_repeat() -> None:
    pairs = pair_users([1, 2, 3, 4], recent((3, 4), (1, 3), (2, 4)))
    repeats = [pair for pair in pairs if frozenset(pair) in recent((3, 4), (1, 3), (2, 4))]
    assert len(pairs) == 2
    assert len(repeats) == 0  # 1-4 and 2-3 are both fresh


def test_only_allowed_pairs_are_made() -> None:
    # 1 may only meet 4 (as a paid search would); the rest pair as usual.
    def can_pair(first: int, second: int) -> bool:
        return 1 not in (first, second) or 4 in (first, second)

    assert pair_users([1, 2, 3, 4], set(), can_pair) == [(1, 4), (2, 3)]


def test_avoiding_a_repeat_never_makes_a_disallowed_pair() -> None:
    # 3-4 met recently. Swapping to 1-3 and 2-4 would avoid the repeat, but 1 may only
    # meet 2, so the repeat stands rather than an unwanted match.
    def can_pair(first: int, second: int) -> bool:
        return 1 not in (first, second) or 2 in (first, second)

    assert pair_users([1, 2, 3, 4], recent((3, 4)), can_pair) == [(1, 2), (3, 4)]


def test_vips_go_first_in_waiting_order() -> None:
    assert vip_first([1, 2, 3, 4, 5], {4, 2}) == [2, 4, 1, 3, 5]
    assert vip_first([1, 2], set()) == [1, 2]


def test_a_vip_is_paired_before_others_and_still_only_where_allowed() -> None:
    # 3 is VIP. 3 can't meet 1 (say, a gender search), so 3 meets 2 and 1 waits.
    order = vip_first([1, 2, 3], {3})
    assert pair_users(order, set(), lambda x, y: {x, y} != {1, 3}) == [(3, 2)]
