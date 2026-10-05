"""多人公共汇合点选择。

一次请求给出多名访客（各自的起点、最早出发时刻、避台阶标记）、非空候选
节点列表与共同的最晚汇合时刻。每名访客独立按现有封闭、等候与最早到达
规则通行（复用 :class:`~app.routing.RouteEngine`），抵达汇合点后可在
节点等待其他人。

可行候选
========
只有**所有**访客都能在最晚汇合时刻（含该时刻，闭区间语义）前抵达的
候选节点才合格；任一访客不可达或抵达更晚则该候选淘汰。

择优顺序
========
1. 全员实际汇合时刻（所有人抵达时刻的最大值）最早；
2. 平局时个人抵达时刻总和最小（纪元秒，与时间轴无关，仅用于择优）；
3. 再平局按候选节点编号字典序。

每人的单人路线平局仍按原路线查询语义——全程路段编号序列字典序——打破。

一次调用固定使用同一个 :class:`~app.db.Snapshot`（即同一数据版本）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Optional

from .routing import NoRouteError, Route, RouteEngine


class Visitor(NamedTuple):
    """一名访客的查询输入（时刻为纪元秒）。"""

    id: str
    origin: str
    departure: int
    avoid_stairs: bool


class CandidateRejection(NamedTuple):
    """候选点对某位访客不可行的原因。"""

    visitor_index: int
    visitor_id: str
    reason: str               # unreachable / late
    arrival: Optional[int]    # 实际最早抵达（late）；不可达时为 None


@dataclass(frozen=True)
class CandidateReport:
    """单个候选节点的评估结果。"""

    node: str
    feasible: bool
    arrivals: tuple[Optional[int], ...]   # 每位访客的最早抵达（不可达为 None）
    meeting_time: Optional[int]           # 全员实际汇合时刻（可行时）
    arrival_sum: Optional[int]            # 个人抵达时刻总和（可行时）
    rejections: tuple[CandidateRejection, ...]


@dataclass(frozen=True)
class VisitorPlan:
    """选定汇合点下某位访客的实际路线与到达后等待。"""

    index: int
    visitor: Visitor
    route: Route

    @property
    def arrival_time(self) -> int:
        return self.route.arrival_time

    @property
    def waiting_after_arrival(self) -> int:
        """到达汇合点后等待其他人的秒数（汇合时刻 - 个人抵达时刻）。"""
        return self.meeting_time - self.route.arrival_time

    meeting_time: int = 0


class NoEligibleCandidateError(Exception):
    """没有任何候选点能让所有人在最晚汇合时刻前抵达。"""


@dataclass(frozen=True)
class MeetingPlan:
    meeting_node: str
    meeting_time: int            # 全员实际汇合时刻
    deadline: int                # 共同的最晚汇合时刻
    visitors: tuple[Visitor, ...]
    plans: tuple[VisitorPlan, ...]
    reports: tuple[CandidateReport, ...]
    data_version: int

    @property
    def arrival_sum(self) -> int:
        return sum(p.arrival_time for p in self.plans)


def _evaluate_candidate(
    engine: RouteEngine,
    node: str,
    visitors: tuple[Visitor, ...],
    deadline: int,
) -> CandidateReport:
    """评估单个候选点：每位访客只需一次最早到达映射。"""
    arrivals: list[Optional[int]] = []
    rejections: list[CandidateRejection] = []
    for i, v in enumerate(visitors):
        dist = engine.earliest_arrivals(v.origin, v.departure, v.avoid_stairs)
        arrival = dist.get(node)
        if arrival is None:
            arrivals.append(None)
            rejections.append(
                CandidateRejection(i, v.id, "unreachable", None)
            )
        elif arrival > deadline:
            arrivals.append(arrival)
            rejections.append(
                CandidateRejection(i, v.id, "late", arrival)
            )
        else:
            arrivals.append(arrival)

    if rejections:
        return CandidateReport(node, False, tuple(arrivals), None, None,
                               tuple(rejections))
    known = [a for a in arrivals if a is not None]
    meeting_time = max(known)
    return CandidateReport(
        node=node,
        feasible=True,
        arrivals=tuple(arrivals),
        meeting_time=meeting_time,
        arrival_sum=sum(known),
        rejections=(),
    )


def plan_meeting(
    engine: RouteEngine,
    visitors: tuple[Visitor, ...],
    candidates: tuple[str, ...],
    deadline: int,
) -> MeetingPlan:
    """在候选节点中选出最优公共汇合点，并重放每人的获胜路线。"""
    reports = tuple(
        _evaluate_candidate(engine, node, visitors, deadline) for node in candidates
    )
    eligible = [r for r in reports if r.feasible]
    if not eligible:
        reasons: list[str] = []
        for r in reports:
            detail = "、".join(
                (f"访客 {rej.visitor_id} 不可达" if rej.reason == "unreachable"
                 else f"访客 {rej.visitor_id} 抵达晚于最晚汇合时刻")
                for rej in r.rejections
            )
            reasons.append(f"{r.node}（{detail}）")
        raise NoEligibleCandidateError(
            "没有候选点能让所有访客在最晚汇合时刻前抵达：" + "；".join(reasons)
        )

    winner = min(
        eligible,
        key=lambda r: (r.meeting_time, r.arrival_sum, r.node),
    )

    # 对获胜点逐人重放：plan() 复现单人路线的字典序平局语义与逐段时间
    plans: list[VisitorPlan] = []
    for i, v in enumerate(visitors):
        try:
            route = engine.plan(
                origin=v.origin,
                destination=winner.node,
                departure=v.departure,
                avoid_stairs=v.avoid_stairs,
            )
        except NoRouteError:
            # 第一阶段已确认可达，重放失败属于内部错误，不应发生
            raise
        plans.append(
            VisitorPlan(
                index=i,
                visitor=v,
                route=route,
                meeting_time=winner.meeting_time,
            )
        )

    return MeetingPlan(
        meeting_node=winner.node,
        meeting_time=winner.meeting_time,
        deadline=deadline,
        visitors=visitors,
        plans=tuple(plans),
        reports=reports,
        data_version=engine.version_id,
    )
