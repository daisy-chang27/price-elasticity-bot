import os
import re
import datetime
import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional
from pydantic import BaseModel, Field

from google.adk.agents import Agent, LlmAgent
from google.adk.tools import AgentTool
from google.adk.workflow import Workflow, START
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.genai import types
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

from app.config import config

# Setup logging
logger = logging.getLogger("price_elasticity_bot")
logger.setLevel(logging.INFO)

# Structured pricing recommendation schema
class PricingRecommendation(BaseModel):
    sku: str = Field(description="The product SKU under review")
    current_price: float = Field(description="The current selling price of the product")
    competitor_average_price: float = Field(description="The average competitor price found")
    elasticity_coefficient: float = Field(description="Estimated price elasticity coefficient (e.g. -1.2)")
    recommended_price: float = Field(description="The newly recommended price")
    needs_approval: bool = Field(description="True if the change exceeds 10% or requires PM review")
    justification: str = Field(description="Brief explanation of the recommendation and elasticity findings")

# 1. MCP Tools Setup
mcp_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "app.mcp_server"],
        )
    )
)

# 2. Specialized LlmAgents (Sub-agents)
competitor_analyzer = LlmAgent(
    name="competitor_analyzer",
    model=config.model,
    instruction="""You are a competitor price analysis specialist.
Your job is to analyze competitor prices for a given SKU.
Use the `get_competitor_prices` tool to fetch competitor pricing data.
Summarize the competitor prices, calculate the average competitor price, and return this analysis.""",
    description="Analyzes competitor pricing for a given SKU.",
    tools=[mcp_tools],
)

elasticity_calculator = LlmAgent(
    name="elasticity_calculator",
    model=config.model,
    instruction="""You are a price elasticity calculation specialist.
Your job is to fetch historical sales and pricing data for a SKU and estimate price elasticity of demand.
Use the `get_product_sales_history` tool to get sales volumes and prices over time.
Estimate elasticity (percentage change in quantity / percentage change in price).
Provide a brief analysis of how price changes affect demand, and return this analysis.""",
    description="Calculates price elasticity and historical sales trends for a given SKU.",
    tools=[mcp_tools],
)

# 3. Main Orchestrator Agent
orchestrator_agent = LlmAgent(
    name="orchestrator_agent",
    model=config.model,
    instruction="""You are the main coordinator for the Price Elasticity Bot.
Your task is to analyze price elasticity and competitor prices for a SKU and recommend an optimal price.

The SKU under review is: {sku}
The user's original query: {query}

Follow these steps:
1. Delegate to competitor_analyzer (using the competitor_analyzer tool) to analyze competitor prices for the SKU.
2. Delegate to elasticity_calculator (using the elasticity_calculator tool) to check the sales history and estimate price elasticity.
3. Based on the competitor average price, current price, and price elasticity coefficient, calculate a new recommended price:
   - If demand is highly elastic (coefficient < -1.5) and our price is higher than competitor average, consider lowering our price.
   - If demand is inelastic (coefficient between -1.0 and 0) and we are below competitor average, consider raising our price.
   - Otherwise, suggest a price closer to the competitor average.
4. Set `needs_approval` to True if:
   - The recommended price change is more than 10% from the current price.
   - The SKU is a high-volume item or demand is highly elastic.
5. Provide a detailed justification for the recommendation.

Ensure you call the tools to delegate work, gather all the data, and output a final response. You MUST use output_schema to return the structured PricingRecommendation.""",
    tools=[AgentTool(competitor_analyzer), AgentTool(elasticity_calculator)],
    output_schema=PricingRecommendation,
)

# 4. Pricing Executor Agent (applies the price change if approved)
pricing_executor = LlmAgent(
    name="pricing_executor",
    model=config.model,
    instruction="""You are the pricing implementation agent.
Your task is to finalize and apply the pricing recommendation.
Read the pm_approval_status from the state:
- If pm_approval_status is 'denied', do NOT call any tools. Output a message stating that the price change was denied by the Product Manager and no changes were made.
- If pm_approval_status is 'approved' or not set (auto-approved), call the `update_recommended_price` tool to update the recommended price for the SKU. Then output a success message confirming the price has been updated to the database.
Summarize what actions were taken.""",
    tools=[mcp_tools],
)

# 5. Workflow Node Functions

def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    """PII Scrubbing and Prompt Injection Safety Node."""
    # Extract text input
    if hasattr(node_input, "parts") and node_input.parts:
        text = "".join([p.text for p in node_input.parts if p.text])
    elif isinstance(node_input, str):
        text = node_input
    elif isinstance(node_input, dict) and "text" in node_input:
        text = node_input["text"]
    else:
        text = str(node_input)

    # Audit logging helper
    def log_audit(severity: str, action: str, details: str):
        log_entry = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "severity": severity,
            "action": action,
            "details": details
        }
        if "audit_logs" not in ctx.state:
            ctx.state["audit_logs"] = []
        ctx.state["audit_logs"].append(log_entry)
        logger.info(json.dumps(log_entry))

    log_audit("INFO", "security_checkpoint_start", f"Processing input: {text[:100]}")

    # Prompt Injection Detection
    injection_keywords = [
        "ignore previous instructions",
        "system prompt",
        "override role",
        "bypass rules",
        "act as developer",
        "dan mode"
    ]
    for keyword in injection_keywords:
        if keyword in text.lower():
            log_audit("CRITICAL", "prompt_injection_detected", f"Found keyword: {keyword}")
            return Event(
                output=f"Security Alert: Potential prompt injection detected (keyword: '{keyword}'). Process aborted.",
                route="SECURITY_EVENT"
            )

    # PII Scrubbing (Email, Credit Card, Name patterns)
    email_regex = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
    cc_regex = r"\b(?:\d[ -]*?){13,16}\b"
    name_regex = r"(?i)\bmy name is\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\b"

    scrubbed_text = text
    if config.pii_redaction_enabled:
        if re.search(email_regex, scrubbed_text):
            scrubbed_text = re.sub(email_regex, "[REDACTED_EMAIL]", scrubbed_text)
            log_audit("WARNING", "pii_scrubbed", "Email address redacted from input")
        if re.search(cc_regex, scrubbed_text):
            scrubbed_text = re.sub(cc_regex, "[REDACTED_CC]", scrubbed_text)
            log_audit("WARNING", "pii_scrubbed", "Credit card number redacted from input")
        if re.search(name_regex, scrubbed_text):
            scrubbed_text = re.sub(name_regex, "My name is [REDACTED_NAME]", scrubbed_text)
            log_audit("WARNING", "pii_scrubbed", "Customer name redacted from input")

    # Domain specific rule: Extract SKU
    sku_match = re.search(r"SKU-\d+", scrubbed_text, re.IGNORECASE)
    if sku_match:
        sku = sku_match.group(0).upper()
        ctx.state["sku"] = sku
        log_audit("INFO", "sku_extracted", f"Extracted SKU: {sku}")
    else:
        # Default SKU for local dev testing if not provided
        sku = "SKU-100"
        ctx.state["sku"] = sku
        log_audit("INFO", "sku_default", f"No SKU found, defaulting to: {sku}")

    log_audit("INFO", "security_checkpoint_passed", "Input passed safety checks")
    return Event(
        output=scrubbed_text,
        route="SAFE",
        state={"query": scrubbed_text, "sku": sku}
    )

def security_alert_handler(node_input: str) -> Event:
    """Terminal node for security alerts."""
    return Event(
        output=node_input,
        content=types.Content(role='model', parts=[types.Part.from_text(text=node_input)])
    )

def decision_gate(ctx: Context, node_input: Any) -> Event:
    """Examines orchestrator recommendations and applies domain specific controls."""
    recommendation = node_input
    if isinstance(recommendation, BaseModel):
        rec_dict = recommendation.model_dump()
    elif isinstance(recommendation, dict):
        rec_dict = recommendation
    else:
        rec_dict = {
            "sku": ctx.state.get("sku", "UNKNOWN"),
            "current_price": 100.0,
            "competitor_average_price": 95.0,
            "elasticity_coefficient": -1.2,
            "recommended_price": 95.0,
            "needs_approval": True,
            "justification": "Fallback pricing recommendation."
        }

    # Save to state
    for k, v in rec_dict.items():
        ctx.state[k] = v

    def log_audit(severity: str, action: str, details: str):
        log_entry = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "severity": severity,
            "action": action,
            "details": details
        }
        if "audit_logs" not in ctx.state:
            ctx.state["audit_logs"] = []
        ctx.state["audit_logs"].append(log_entry)
        logger.info(json.dumps(log_entry))

    # Domain specific rule: Price change threshold check
    curr = rec_dict.get("current_price", 100.0)
    reco = rec_dict.get("recommended_price", 100.0)
    change_pct = abs(reco - curr) / curr if curr > 0 else 0

    # If price change > 50%, force approval regardless of agent decision
    if change_pct > 0.50:
        rec_dict["needs_approval"] = True
        ctx.state["needs_approval"] = True
        log_audit("WARNING", "price_change_limit_exceeded", 
                  f"Recommended price change of {change_pct:.1%} exceeds 50% threshold. Forcing PM review.")

    # Determine next routing path
    if rec_dict.get("needs_approval"):
        log_audit("INFO", "routing_decision", f"SKU {rec_dict['sku']} requires PM approval. Routing to HITL.")
        return Event(output=rec_dict, route="NEEDS_APPROVAL")
    else:
        log_audit("INFO", "routing_decision", f"SKU {rec_dict['sku']} auto-approved. Routing directly to executor.")
        return Event(output=rec_dict, route="AUTO_APPROVED")

async def hitl_approval(ctx: Context, node_input: Any):
    """Human-in-the-loop product manager approval node."""
    recommendation = node_input
    sku = recommendation.get("sku", "UNKNOWN")
    reco_price = recommendation.get("recommended_price", 0.0)
    curr_price = recommendation.get("current_price", 0.0)
    justification = recommendation.get("justification", "")

    def log_audit(severity: str, action: str, details: str):
        log_entry = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "severity": severity,
            "action": action,
            "details": details
        }
        if "audit_logs" not in ctx.state:
            ctx.state["audit_logs"] = []
        ctx.state["audit_logs"].append(log_entry)
        logger.info(json.dumps(log_entry))

    # Yield RequestInput if PM response not yet received
    if not ctx.resume_inputs:
        log_audit("INFO", "hitl_approval_requested", f"Awaiting PM approval for {sku}")
        yield RequestInput(
            interrupt_id="pm_approval",
            message=f"✋ **Pricing Review Required for {sku}**\n\n"
                    f"- **Current Price:** ${curr_price:.2f}\n"
                    f"- **Recommended Price:** ${reco_price:.2f}\n"
                    f"- **Justification:** {justification}\n\n"
                    f"Please reply with: **approve** or **deny**"
        )
        return

    # Check the decision upon resume
    user_decision = ctx.resume_inputs.get("pm_approval", "").strip().lower()
    log_audit("INFO", "hitl_approval_received", f"PM decision received: '{user_decision}'")

    if "approve" in user_decision:
        recommendation["pm_approval_status"] = "approved"
        yield Event(
            output=recommendation,
            state={"pm_approval_status": "approved"}
        )
    else:
        recommendation["pm_approval_status"] = "denied"
        yield Event(
            output=recommendation,
            state={"pm_approval_status": "denied"}
        )

# 6. Workflow Graph Definition
root_agent = Workflow(
    name="price_elasticity_bot_workflow",
    edges=[
        (START, security_checkpoint),
        (security_checkpoint, {'SECURITY_EVENT': security_alert_handler, 'SAFE': orchestrator_agent}),
        (orchestrator_agent, decision_gate),
        (decision_gate, {'NEEDS_APPROVAL': hitl_approval, 'AUTO_APPROVED': pricing_executor}),
        (hitl_approval, pricing_executor),
    ]
)


# 7. App Configuration
app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True)
)
