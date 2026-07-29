from __future__ import annotations


FIXED_RULE_ORDER: list[tuple[str, str]] = [
    ("hot", "\u71b1\u9580"),
    ("cold", "\u51b7\u9580"),
    ("missing", "\u7f3a\u865f"),
    ("repeat", "\u91cd\u865f"),
    ("tail", "\u5c3e\u6578"),
    ("gap", "\u9593\u8ddd"),
    ("cluster", "\u7fa4\u805a"),
    ("diagonal", "\u659c\u7dda"),
    ("super", "\u8d85\u7d1a\u734e"),
    ("laowanjia", "\u8001\u73a9\u5bb6"),
    ("ladder", "\u968e\u68af"),
    ("partial_ladder", "\u504f\u968e"),
    ("extended_ladder", "\u5ef6\u968e"),
    ("reverse", "\u53cd\u865f"),
    ("neighbor", "\u9694\u58c1\u865f"),
    ("guide", "\u5f15\u8def\u724c"),
    ("integrated", "\u6574\u5408\u6578"),
    ("sunset", "\u592a\u967d\u4e0b\u5c71"),
    ("momentum", "\u76e4\u52e2\u52d5\u80fd"),
    ("super_number_trajectory_recovery", "super_number_trajectory_recovery"),
    ("cluster_aftershock_recovery", "cluster_aftershock_recovery"),
]


RULE_NAME_ZH = dict(FIXED_RULE_ORDER)


def fixed_rule_keys() -> list[str]:
    return [key for key, _ in FIXED_RULE_ORDER]

