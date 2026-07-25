"""Optional standalone news-sentiment scoring prompt.

NOT called in the default cycle - maverick.get_news_sentiment() already returns a
sentiment score/label in a single batched call (see mcp/maverick.py). This template
exists for cases where you want a SEPARATE, deeper qualitative read of the raw
headlines already sitting in TickerData.news_headlines (e.g. an on-demand "explain
this stock's news" command), without spending another MCP round-trip - it just runs
Claude's own reasoning over headlines you already fetched.
"""


def build_sentiment_prompt(ticker: str, headlines: list[dict]) -> str:
    lines = "\n".join(f"- {h.get('title', '')}: {h.get('summary', '')}" for h in headlines[:10])
    return f"""Score the aggregate news sentiment for {ticker} based on these recent headlines:

{lines or "(no headlines available)"}

Respond ONLY with this JSON (no markdown, no explanation):
{{"sentiment_score": 0.0, "sentiment_label": "positive|neutral|negative", "key_themes": ["theme1", "theme2"]}}"""
