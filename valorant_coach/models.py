from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Match:
    id: int
    video_path: str
    started_at: str
    duration: Optional[float]
    map: Optional[str]
    agent: Optional[str]
    status: str
    created_at: str


@dataclass
class Round:
    id: int
    match_id: int
    round_number: int
    start_ts: Optional[float]
    end_ts: Optional[float]
    outcome: Optional[str]
    side: Optional[str]


@dataclass
class DeathEvent:
    id: int
    match_id: int
    round_number: Optional[int]
    timestamp: Optional[float]
    clip_path: Optional[str]
    mistake_labels: List[str]
    confidence: float
    notes: str

