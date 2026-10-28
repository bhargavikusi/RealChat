"""Pairing rules for waiting users (D-033)."""

from backend.modules.matching.matcher import pair_users


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
