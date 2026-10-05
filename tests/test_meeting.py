"""多人汇合点选择测试：可行性、择优顺序、避台阶/封闭、等待与输入校验。"""
import unittest

from app.db import ClosureRow, EdgeRow, Snapshot
from app.routing import RouteEngine
from app.meeting import NoEligibleCandidateError, Visitor, plan_meeting
from app.schemas import ValidationError, normalize_meeting


def make_engine(nodes, edges, closures=(), version_id=1):
    return RouteEngine(
        Snapshot(
            version_id=version_id,
            nodes=list(nodes),
            edges=[EdgeRow(*e) for e in edges],
            closures=[ClosureRow(*c) for c in closures],
        )
    )


# (id, src, dst, seconds, has_stairs)
def E(eid, u, v, secs, stairs=False):
    return (eid, u, v, secs, stairs)


def V(i, origin, dep=0, avoid=False, vid=None):
    return Visitor(vid if vid is not None else f"p{i}", origin, dep, avoid)


class MeetingSelectionTests(unittest.TestCase):
    def test_picks_earliest_meeting(self):
        # v0: S1; v1: S2。在 A 会合 10（10,10），在 B 会合 30（30,10）
        eng = make_engine(
            ["S1", "S2", "A", "B"],
            [E("E1", "S1", "A", 10), E("E2", "S2", "A", 10),
             E("E3", "S1", "B", 30), E("E4", "S2", "B", 10)],
        )
        plan = plan_meeting(eng, (V(0, "S1"), V(1, "S2")), ("A", "B"), 1000)
        self.assertEqual(plan.meeting_node, "A")
        self.assertEqual(plan.meeting_time, 10)
        self.assertEqual(plan.arrival_sum, 20)
        self.assertEqual([p.route.arrival_time for p in plan.plans], [10, 10])
        self.assertEqual([p.waiting_after_arrival for p in plan.plans], [0, 0])

    def test_max_arrival_primary_over_sum(self):
        # A: 抵达 (100,90) → 汇合 100；B: 抵达 (50,50) → 汇合 50。
        # B 的抵达总和 100 反而更大，但汇合时刻更早 → 选 B
        eng = make_engine(
            ["S1", "S2", "A", "B"],
            [E("E1", "S1", "A", 100), E("E2", "S2", "A", 90),
             E("E3", "S1", "B", 50), E("E4", "S2", "B", 50)],
        )
        plan = plan_meeting(eng, (V(0, "S1"), V(1, "S2")), ("A", "B"), 1000)
        self.assertEqual(plan.meeting_node, "B")
        self.assertEqual(plan.meeting_time, 50)

    def test_arrival_sum_is_second_tiebreak(self):
        # A: (100,90) 总和 190；B: (100,50) 总和 150，汇合时刻同为 100 → 选 B
        eng = make_engine(
            ["S1", "S2", "A", "B"],
            [E("E1", "S1", "A", 100), E("E2", "S2", "A", 90),
             E("E3", "S1", "B", 100), E("E4", "S2", "B", 50)],
        )
        plan = plan_meeting(eng, (V(0, "S1"), V(1, "S2")), ("A", "B"), 1000)
        self.assertEqual(plan.meeting_node, "B")
        self.assertEqual(plan.meeting_time, 100)
        self.assertEqual(plan.arrival_sum, 150)

    def test_node_id_lex_third_tiebreak(self):
        eng = make_engine(
            ["S1", "S2", "A", "B"],
            [E("E1", "S1", "A", 100), E("E2", "S2", "A", 50),
             E("E3", "S1", "B", 100), E("E4", "S2", "B", 50)],
        )
        # 候选顺序倒置不影响结果
        plan = plan_meeting(eng, (V(0, "S1"), V(1, "S2")), ("B", "A"), 1000)
        self.assertEqual(plan.meeting_node, "A")
        self.assertEqual(plan.arrival_sum, 150)

    def test_arrival_exactly_at_deadline_allowed(self):
        eng = make_engine(["S", "T"], [E("E1", "S", "T", 60)])
        plan = plan_meeting(
            eng, (V(0, "S"), V(1, "T")), ("T",), 60
        )
        self.assertEqual(plan.meeting_node, "T")
        self.assertEqual(plan.meeting_time, 60)

    def test_late_arrival_rejects_candidate(self):
        eng = make_engine(["S1", "S2", "A", "B"],
                          [E("E1", "S1", "A", 10), E("E2", "S2", "A", 10),
                           E("E3", "S1", "B", 30), E("E4", "S2", "B", 10)])
        # B 对 v0 需 30 > deadline 20 → 仅 A 合格
        plan = plan_meeting(eng, (V(0, "S1"), V(1, "S2")), ("A", "B"), 20)
        self.assertEqual(plan.meeting_node, "A")
        report_b = {r.node: r for r in plan.reports}["B"]
        self.assertFalse(report_b.feasible)
        (rej,) = report_b.rejections
        self.assertEqual((rej.visitor_index, rej.visitor_id, rej.reason),
                         (0, "p0", "late"))
        self.assertEqual(rej.arrival, 30)

    def test_unreachable_candidate_rejected(self):
        eng = make_engine(["S1", "S2", "A"],
                          [E("E1", "S1", "A", 10)])
        with self.assertRaises(NoEligibleCandidateError):
            plan_meeting(eng, (V(0, "S1"), V(1, "S2")), ("A",), 1000)

    def test_no_candidate_before_deadline_is_error(self):
        eng = make_engine(["S", "T"], [E("E1", "S", "T", 60)])
        with self.assertRaises(NoEligibleCandidateError):
            plan_meeting(eng, (V(0, "S"), V(1, "S")), ("T",), 59)

    def test_avoid_stairs_per_visitor(self):
        # v0 避台阶：直达台阶 E1(10s) 不可用，只能 E2+E3 平路（共 10s）
        eng = make_engine(
            ["S1", "S2", "A", "X"],
            [E("E1", "S1", "A", 10, stairs=True),
             E("E2", "S1", "X", 5, stairs=False),
             E("E3", "X", "A", 5, stairs=False),
             E("E4", "S2", "A", 20)],
        )
        plan = plan_meeting(
            eng, (V(0, "S1", avoid=True), V(1, "S2")), ("A",), 1000
        )
        self.assertEqual(plan.plans[0].route.arrival_time, 10)
        self.assertEqual(
            [s.edge_id for s in plan.plans[0].route.segments], ["E2", "E3"]
        )
        self.assertTrue(all(not s.has_stairs for s in plan.plans[0].route.segments))
        self.assertEqual(plan.plans[1].route.arrival_time, 20)
        self.assertEqual(plan.meeting_time, 20)
        # v0 早到 10，等到 20 → 等待 10 秒
        self.assertEqual(plan.plans[0].waiting_after_arrival, 10)
        self.assertEqual(plan.plans[1].waiting_after_arrival, 0)

    def test_closures_and_node_waiting_respected(self):
        # E1 在 [0,60) 封闭：0 时刻出发需等到 60 进入，120 抵达
        eng = make_engine(["S", "T"], [E("E1", "S", "T", 60)],
                          closures=[("E1", 0, 60)])
        plan = plan_meeting(eng, (V(0, "S"), V(1, "T")), ("T",), 120)
        seg = plan.plans[0].route.segments[0]
        self.assertEqual((seg.enter_time, seg.leave_time), (60, 120))
        self.assertEqual(seg.waiting_seconds, 60)
        self.assertEqual(plan.plans[1].waiting_after_arrival, 120)

    def test_individual_route_uses_full_edge_sequence_lex_tie(self):
        # 与路由引擎字典序测试相同图：E4 [10,60) 封把两条路线拉平到 160
        edges = [
            E("E2", "S", "A", 10), E("E4", "A", "T", 100),
            E("E1", "S", "B", 50), E("E3", "B", "T", 110),
        ]
        eng = make_engine(["S", "A", "B", "T"], edges, [("E4", 10, 60)])
        plan = plan_meeting(eng, (V(0, "S"), V(1, "T")), ("T",), 200)
        self.assertEqual(
            [s.edge_id for s in plan.plans[0].route.segments], ["E1", "E3"]
        )
        self.assertEqual(plan.meeting_time, 160)
        # 第二位访客起点即汇合点，到达后等待 160 秒
        self.assertEqual(plan.plans[1].route.segments, ())
        self.assertEqual(plan.plans[1].waiting_after_arrival, 160)

    def test_data_version_propagated(self):
        eng = make_engine(["S1", "S2", "A"],
                          [E("E1", "S1", "A", 10), E("E2", "S2", "A", 10)],
                          version_id=7)
        plan = plan_meeting(eng, (V(0, "S1"), V(1, "S2")), ("A",), 100)
        self.assertEqual(plan.data_version, 7)

    def test_reports_cover_all_candidates_in_request_order(self):
        eng = make_engine(
            ["S1", "S2", "A", "B"],
            [E("E1", "S1", "A", 10), E("E2", "S2", "A", 10),
             E("E3", "S1", "B", 30), E("E4", "S2", "B", 10)],
        )
        plan = plan_meeting(eng, (V(0, "S1"), V(1, "S2")), ("B", "A"), 1000)
        self.assertEqual([r.node for r in plan.reports], ["B", "A"])
        by_node = {r.node: r for r in plan.reports}
        self.assertEqual(by_node["A"].arrivals, (10, 10))
        self.assertEqual(by_node["B"].arrivals, (30, 10))


class MeetingPayloadValidationTests(unittest.TestCase):
    NODES = {"S1", "S2", "A", "B"}

    def base_payload(self, **overrides):
        payload = {
            "visitors": [
                {"origin": "S1", "departure_time": "2026-10-05T08:00:00Z"},
                {"origin": "S2", "departure_time": "2026-10-05T08:00:00Z",
                 "avoid_stairs": True, "id": "bob"},
            ],
            "candidates": ["A", "B"],
            "latest_meeting_time": "2026-10-05T10:00:00Z",
        }
        payload.update(overrides)
        return payload

    def test_valid_payload(self):
        q = normalize_meeting(self.base_payload(), self.NODES)
        self.assertEqual([v["origin"] for v in q["visitors"]], ["S1", "S2"])
        self.assertEqual([v["id"] for v in q["visitors"]], ["0", "bob"])
        self.assertFalse(q["visitors"][0]["avoid_stairs"])
        self.assertTrue(q["visitors"][1]["avoid_stairs"])
        self.assertEqual(q["candidates"], ["A", "B"])
        self.assertIsInstance(q["deadline"], int)

    def test_alias_fields_accepted(self):
        payload = {
            "people": [
                {"start": "S1", "earliest_departure": 1000},
                {"from": "S2", "departure": 2000, "stairs_free": True},
            ],
            "candidate_nodes": ["A"],
            "meeting_deadline": 5000,
        }
        q = normalize_meeting(payload, self.NODES)
        self.assertEqual(q["visitors"][0]["origin"], "S1")
        self.assertEqual(q["visitors"][0]["departure"], 1000)
        self.assertEqual(q["visitors"][1]["origin"], "S2")
        self.assertTrue(q["visitors"][1]["avoid_stairs"])
        self.assertEqual(q["candidates"], ["A"])
        self.assertEqual(q["deadline"], 5000)

    def test_conflicting_aliases_rejected(self):
        payload = self.base_payload()
        payload["deadline"] = "2026-10-05T11:00:00Z"  # 与 latest_meeting_time 不一致
        with self.assertRaises(ValidationError):
            normalize_meeting(payload, self.NODES)

    def test_fewer_than_two_visitors_rejected(self):
        payload = self.base_payload()
        payload["visitors"] = payload["visitors"][:1]
        with self.assertRaises(ValidationError):
            normalize_meeting(payload, self.NODES)

    def test_visitors_must_be_list(self):
        payload = self.base_payload(visitors={"origin": "S1"})
        with self.assertRaises(ValidationError):
            normalize_meeting(payload, self.NODES)

    def test_empty_candidates_rejected(self):
        with self.assertRaises(ValidationError):
            normalize_meeting(self.base_payload(candidates=[]), self.NODES)

    def test_duplicate_candidate_rejected(self):
        with self.assertRaises(ValidationError):
            normalize_meeting(self.base_payload(candidates=["A", "A"]), self.NODES)

    def test_duplicate_visitor_id_rejected(self):
        payload = self.base_payload()
        payload["visitors"][1]["id"] = "0"
        with self.assertRaises(ValidationError):
            normalize_meeting(payload, self.NODES)

    def test_unknown_visitor_origin_rejected(self):
        payload = self.base_payload()
        payload["visitors"][0]["origin"] = "GHOST"
        with self.assertRaises(ValidationError):
            normalize_meeting(payload, self.NODES)

    def test_unknown_candidate_rejected(self):
        with self.assertRaises(ValidationError):
            normalize_meeting(self.base_payload(candidates=["A", "GHOST"]), self.NODES)

    def test_bad_visitor_time_rejected(self):
        payload = self.base_payload()
        payload["visitors"][0]["departure_time"] = "not-a-time"
        with self.assertRaises(ValidationError):
            normalize_meeting(payload, self.NODES)

    def test_bad_deadline_rejected(self):
        with self.assertRaises(ValidationError):
            normalize_meeting(
                self.base_payload(latest_meeting_time=1.5), self.NODES
            )

    def test_non_bool_avoid_stairs_rejected(self):
        payload = self.base_payload()
        payload["visitors"][0]["avoid_stairs"] = "yes"
        with self.assertRaises(ValidationError):
            normalize_meeting(payload, self.NODES)

    def test_missing_required_fields_rejected(self):
        for field in ("visitors", "candidates", "latest_meeting_time"):
            payload = self.base_payload()
            del payload[field]
            with self.assertRaises(ValidationError, msg=f"missing {field}"):
                normalize_meeting(payload, self.NODES)

    def test_payload_must_be_object(self):
        with self.assertRaises(ValidationError):
            normalize_meeting(["not", "object"], self.NODES)

    def test_visitor_must_be_object(self):
        payload = self.base_payload()
        payload["visitors"][0] = "S1"
        with self.assertRaises(ValidationError):
            normalize_meeting(payload, self.NODES)

    def test_epoch_seconds_accepted(self):
        payload = self.base_payload(latest_meeting_time=5000)
        payload["visitors"][0]["departure_time"] = 100
        q = normalize_meeting(payload, self.NODES)
        self.assertEqual(q["deadline"], 5000)
        self.assertEqual(q["visitors"][0]["departure"], 100)


if __name__ == "__main__":
    unittest.main()
