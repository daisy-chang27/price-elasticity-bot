import sys
import json
from mcp.server.fastmcp import FastMCP

# Create MCP server
mcp = FastMCP("Price Elasticity Analytics Server")

# Mock database
MOCK_PRODUCTS = {
    "SKU-100": {
        "name": "Wireless Noise-Canceling Headphones",
        "current_price": 100.0,
        "sales_history": [
            {"week": 1, "price": 100.0, "volume": 150},
            {"week": 2, "price": 110.0, "volume": 100},
            {"week": 3, "price": 95.0, "volume": 180},
            {"week": 4, "price": 100.0, "volume": 145}
        ],
        "competitors": {
            "AudioMax": 98.0,
            "SoundWave": 96.50,
            "BassBoost": 102.0
        }
    },
    "SKU-200": {
        "name": "Smart Fitness Watch",
        "current_price": 200.0,
        "sales_history": [
            {"week": 1, "price": 200.0, "volume": 80},
            {"week": 2, "price": 220.0, "volume": 50},
            {"week": 3, "price": 180.0, "volume": 120},
            {"week": 4, "price": 200.0, "volume": 85}
        ],
        "competitors": {
            "FitTech": 195.0,
            "ActiveLife": 189.0,
            "PulseGear": 210.0
        }
    },
    "SKU-300": {
        "name": "Ergonomic Office Chair",
        "current_price": 350.0,
        "sales_history": [
            {"week": 1, "price": 350.0, "volume": 30},
            {"week": 2, "price": 370.0, "volume": 28},
            {"week": 3, "price": 330.0, "volume": 32},
            {"week": 4, "price": 350.0, "volume": 29}
        ],
        "competitors": {
            "ComfortSeat": 349.0,
            "PosturePlus": 355.0,
            "OfficeFlex": 340.0
        }
    }
}

@mcp.tool()
def get_product_sales_history(sku: str) -> str:
    """Retrieves the price and sales volume history for a specific product SKU.

    Args:
        sku: The product identifier (e.g. 'SKU-100', 'SKU-200', 'SKU-300').
    """
    sku_upper = sku.upper()
    if sku_upper not in MOCK_PRODUCTS:
        # Fallback to SKU-100 for other SKUs to make testing easy
        sku_upper = "SKU-100"
        
    product = MOCK_PRODUCTS[sku_upper]
    response = {
        "sku": sku_upper,
        "name": product["name"],
        "current_price": product["current_price"],
        "sales_history": product["sales_history"]
    }
    return json.dumps(response)

@mcp.tool()
def get_competitor_prices(sku: str) -> str:
    """Fetches current competitor pricing for a specific product SKU.

    Args:
        sku: The product identifier (e.g. 'SKU-100', 'SKU-200', 'SKU-300').
    """
    sku_upper = sku.upper()
    if sku_upper not in MOCK_PRODUCTS:
        sku_upper = "SKU-100"
        
    product = MOCK_PRODUCTS[sku_upper]
    response = {
        "sku": sku_upper,
        "name": product["name"],
        "competitors": product["competitors"]
    }
    return json.dumps(response)

@mcp.tool()
def update_recommended_price(sku: str, recommended_price: float) -> str:
    """Records the recommended price update to the central database.

    Args:
        sku: The product identifier (e.g. 'SKU-100', 'SKU-200', 'SKU-300').
        recommended_price: The new approved price to update.
    """
    sku_upper = sku.upper()
    print(f"Database pricing update: {sku_upper} price set to ${recommended_price:.2f}", file=sys.stderr)
    response = {
        "status": "success",
        "message": f"Successfully updated recommended price for {sku_upper} to ${recommended_price:.2f} in database.",
        "sku": sku_upper,
        "updated_price": recommended_price
    }
    return json.dumps(response)

if __name__ == "__main__":
    mcp.run(transport="stdio")
