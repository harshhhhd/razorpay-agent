
from typing import Optional, Any
from pydantic import BaseModel, Field, EmailStr


class SettingsUpdate(BaseModel):
    daily_budget_inr: Optional[float] = Field(None, gt=0, description="New daily spend cap in INR")
    max_discount_pct: Optional[float] = Field(None, gt=0, le=100, description="New discount ceiling, 1-100")


class SettingsOut(BaseModel):
    daily_budget_inr: float
    max_discount_pct: float


class AdminStatusOut(BaseModel):
    simulated_failure_enabled: bool
    budget_remaining_inr: float
    daily_budget_inr: float
    max_discount_pct: float


class CampaignOut(BaseModel):
    id: int
    campaign_type: str
    title: str
    description: str
    reasoning: str
    data_snapshot: dict
    discount_pct: float
    audience_estimate: int
    expected_lift_inr: float
    budget_required_inr: float
    status: str
    created_at: str
    decided_at: Optional[str] = None


class CampaignDecisionOut(BaseModel):
    status: str
    offer: Optional[dict] = None


class AnalyzeResultOut(BaseModel):
    created_campaign_ids: list[int]
    stale_data: bool
    orders_analyzed: int


class ProductOut(BaseModel):
    id: int
    name: str
    category: str
    price: float
    stock: int
    units_sold_30d: int
    revenue_30d: float


class CustomerOut(BaseModel):
    id: int
    name: str
    email: str
    segment: str
    order_count: int
    total_spend: float
    last_order_at: Optional[str] = None


class AuditLogOut(BaseModel):
    id: int
    action_id: str
    timestamp: str
    decision: str
    reasoning: str
    data_snapshot: Any
    outcome: str
    human_override: Optional[str] = None