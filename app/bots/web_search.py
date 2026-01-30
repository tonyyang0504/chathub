"""
Web Search Tool for WhatsApp Bot
Provides real-time internet search capabilities
"""

import logging
import json
import re
import time
from typing import List, Dict, Optional
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)


# Tool definition for OpenAI function calling
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the internet for current/real-time information. Use this for ANY question about: weather, news, current events, stock prices, cryptocurrency prices, sports scores, recent updates, or anything requiring up-to-date information that you don't know.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to find information"
                }
            },
            "required": ["query"]
        }
    }
}

# List of all available tools
AVAILABLE_TOOLS = [WEB_SEARCH_TOOL]


def search_duckduckgo(query: str, max_results: int = 5) -> List[Dict]:
    """
    Search using DuckDuckGo via the duckduckgo-search library.
    """
    try:
        from duckduckgo_search import DDGS

        results = []
        with DDGS() as ddgs:
            # Use text search with retry logic
            for attempt in range(3):
                try:
                    search_results = list(ddgs.text(query, max_results=max_results))
                    for r in search_results:
                        results.append({
                            "title": r.get("title", ""),
                            "url": r.get("href", ""),
                            "snippet": r.get("body", "")
                        })
                    break
                except Exception as e:
                    if "Ratelimit" in str(e) and attempt < 2:
                        logger.warning(f"DuckDuckGo rate limited, waiting... (attempt {attempt + 1})")
                        time.sleep(2)
                        continue
                    raise

        logger.info(f"DuckDuckGo search for '{query}' returned {len(results)} results")
        return results

    except Exception as e:
        logger.error(f"DuckDuckGo search error: {e}")
        return []


def search_google_news_rss(query: str, max_results: int = 5) -> List[Dict]:
    """
    Search Google News via RSS feed (free, no API key required).
    """
    try:
        # Google News RSS feed
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            xml = response.text

        results = []

        # Parse RSS XML for news items
        # Extract title and description from <item> elements
        items = re.findall(r'<item>(.*?)</item>', xml, re.DOTALL)

        for item in items[:max_results]:
            title_match = re.search(r'<title>(.*?)</title>', item)
            link_match = re.search(r'<link>(.*?)</link>', item)
            pub_date_match = re.search(r'<pubDate>(.*?)</pubDate>', item)
            source_match = re.search(r'<source[^>]*>(.*?)</source>', item)

            title = title_match.group(1) if title_match else ""
            # Clean HTML entities
            title = title.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&#39;', "'")

            link = link_match.group(1) if link_match else ""
            pub_date = pub_date_match.group(1) if pub_date_match else ""
            source = source_match.group(1) if source_match else ""

            if title:
                results.append({
                    "title": title,
                    "url": link,
                    "snippet": f"Source: {source}" if source else "",
                    "date": pub_date
                })

        logger.info(f"Google News RSS for '{query}' returned {len(results)} results")
        return results

    except Exception as e:
        logger.error(f"Google News RSS error: {e}")
        return []


def search_brave(query: str, max_results: int = 5) -> List[Dict]:
    """
    Search using Brave Search (more lenient rate limits than DuckDuckGo).
    """
    try:
        url = f"https://search.brave.com/search?q={quote_plus(query)}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            html = response.text

        results = []

        # Brave search results pattern
        # Look for result titles and snippets
        title_pattern = r'<div[^>]*class="[^"]*snippet-title[^"]*"[^>]*>.*?<a[^>]*>([^<]+)</a>'
        snippet_pattern = r'<div[^>]*class="[^"]*snippet-description[^"]*"[^>]*>([^<]+)</div>'

        titles = re.findall(title_pattern, html, re.DOTALL)
        snippets = re.findall(snippet_pattern, html, re.DOTALL)

        for i, title in enumerate(titles[:max_results]):
            snippet = snippets[i] if i < len(snippets) else ""
            if title and len(title.strip()) > 3:
                results.append({
                    "title": title.strip(),
                    "url": "",
                    "snippet": snippet.strip()
                })

        logger.info(f"Brave search for '{query}' returned {len(results)} results")
        return results

    except Exception as e:
        logger.error(f"Brave search error: {e}")
        return []


def get_crypto_price(symbol: str = "bitcoin") -> Optional[str]:
    """
    Get cryptocurrency price from CoinGecko API (free, no key required).
    """
    try:
        # Map common names to CoinGecko IDs
        crypto_map = {
            "bitcoin": "bitcoin",
            "btc": "bitcoin",
            "ethereum": "ethereum",
            "eth": "ethereum",
            "solana": "solana",
            "sol": "solana",
            "dogecoin": "dogecoin",
            "doge": "dogecoin",
            "xrp": "ripple",
            "ripple": "ripple",
            "cardano": "cardano",
            "ada": "cardano",
            "bnb": "binancecoin",
            "binance": "binancecoin",
        }

        coin_id = crypto_map.get(symbol.lower(), symbol.lower())

        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd&include_24hr_change=true"

        with httpx.Client(timeout=10.0) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()

        if coin_id in data:
            price = data[coin_id].get("usd", 0)
            change = data[coin_id].get("usd_24h_change", 0)
            change_str = f"+{change:.2f}%" if change >= 0 else f"{change:.2f}%"
            return f"{symbol.upper()} price: ${price:,.2f} USD (24h change: {change_str})"

        return None

    except Exception as e:
        logger.error(f"Crypto price error: {e}")
        return None


def get_weather(location: str) -> Optional[str]:
    """
    Get weather using wttr.in API (free, no key required).
    """
    try:
        url = f"https://wttr.in/{quote_plus(location)}?format=j1"

        with httpx.Client(timeout=10.0) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()

        current = data.get("current_condition", [{}])[0]
        area = data.get("nearest_area", [{}])[0]

        temp_c = current.get("temp_C", "N/A")
        temp_f = current.get("temp_F", "N/A")
        desc = current.get("weatherDesc", [{}])[0].get("value", "N/A")
        humidity = current.get("humidity", "N/A")
        wind_kmph = current.get("windspeedKmph", "N/A")

        city = area.get("areaName", [{}])[0].get("value", location)
        country = area.get("country", [{}])[0].get("value", "")

        return f"Weather in {city}, {country}: {desc}, {temp_c}°C ({temp_f}°F), Humidity: {humidity}%, Wind: {wind_kmph} km/h"

    except Exception as e:
        logger.error(f"Weather error: {e}")
        return None


def web_search(query: str) -> str:
    """
    Main web search function that tries multiple methods.
    """
    results_text = []
    query_lower = query.lower()

    # Check for specific query types and use specialized APIs

    # Cryptocurrency price queries
    crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "dogecoin", "xrp", "price"]
    if any(kw in query_lower for kw in crypto_keywords):
        # Extract crypto name
        for crypto in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "dogecoin", "doge", "xrp", "ripple", "cardano", "ada", "bnb"]:
            if crypto in query_lower:
                price_info = get_crypto_price(crypto)
                if price_info:
                    results_text.append(f"Current Price: {price_info}")
                break

    # Weather queries
    weather_keywords = ["weather", "temperature", "forecast", "rain", "sunny", "cloudy"]
    if any(kw in query_lower for kw in weather_keywords):
        # Try to extract location from query
        location = query_lower

        # Remove common words and numbers to extract just the location
        remove_words = weather_keywords + ["in", "at", "for", "today", "now", "current", "what", "is", "the",
                                           "like", "how", "much", "tell", "me", "whats", "what's", "please",
                                           "january", "february", "march", "april", "may", "june", "july",
                                           "august", "september", "october", "november", "december",
                                           "2024", "2025", "2026", "2027", "20", "21", "22", "23", "24", "25"]
        for kw in remove_words:
            location = re.sub(r'\b' + re.escape(kw) + r'\b', ' ', location)

        # Remove any remaining numbers and special characters
        location = re.sub(r'\d+', ' ', location)
        location = re.sub(r'[?\'"!,.]', ' ', location)
        # Clean up multiple spaces and strip
        location = re.sub(r'\s+', ' ', location).strip()

        if not location or len(location) < 2:
            location = "Dubai"  # Default location

        print(f"[WEB_SEARCH] Weather location extracted: '{location}'")
        weather_info = get_weather(location)
        if weather_info:
            results_text.append(f"Current Weather: {weather_info}")

    # Use Google News RSS as PRIMARY search (most reliable, no rate limits)
    # It works for news AND general queries about current events, people, meetings, etc.
    print(f"[WEB_SEARCH] Searching Google News RSS for: {query}")
    news_results = search_google_news_rss(query, max_results=5)

    if news_results:
        results_text.append("\nLatest Information:")
        for i, news in enumerate(news_results, 1):
            results_text.append(f"\n{i}. {news['title']}")
            if news.get('snippet'):
                results_text.append(f"   {news['snippet']}")
            if news.get('date'):
                results_text.append(f"   Published: {news['date']}")

    # If Google News didn't find anything, try DuckDuckGo/Brave as fallback
    if not news_results:
        print(f"[WEB_SEARCH] Google News returned no results, trying DuckDuckGo...")
        search_results = search_duckduckgo(query, max_results=5)

        if not search_results:
            print(f"[WEB_SEARCH] DuckDuckGo failed, trying Brave...")
            search_results = search_brave(query, max_results=5)

        if search_results:
            results_text.append("\nWeb Search Results:")
            for i, result in enumerate(search_results, 1):
                results_text.append(f"\n{i}. {result['title']}")
                if result['snippet']:
                    results_text.append(f"   {result['snippet']}")

    if not results_text:
        return "No search results found. The search service may be temporarily unavailable."

    return "\n".join(results_text)


def execute_tool(tool_name: str, arguments: dict) -> str:
    """
    Execute a tool by name with given arguments.
    """
    print(f"[WEB_SEARCH] execute_tool called: {tool_name} with args: {arguments}")
    logger.info(f"[WEB_SEARCH] execute_tool called: {tool_name} with args: {arguments}")

    if tool_name == "web_search":
        query = arguments.get("query", "")
        if not query:
            return "Error: No search query provided"
        result = web_search(query)
        print(f"[WEB_SEARCH] Result length: {len(result)} chars")
        logger.info(f"[WEB_SEARCH] Result length: {len(result)} chars")
        return result

    return f"Error: Unknown tool '{tool_name}'"


def process_ai_response_with_tools(openai_client, messages: List[Dict], model: str = "gpt-4o-mini",
                                    max_tokens: int = 2000, temperature: float = 0.7,
                                    top_p: float = 1.0, frequency_penalty: float = 0.0,
                                    presence_penalty: float = 0.0,
                                    max_tool_iterations: int = 3) -> str:
    """
    Process AI response with tool calling support.
    """
    current_messages = messages.copy()
    iterations = 0

    while iterations < max_tool_iterations:
        iterations += 1

        try:
            # Call OpenAI with tools
            response = openai_client.chat.completions.create(
                model=model,
                messages=current_messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
                tools=AVAILABLE_TOOLS,
                tool_choice="auto"
            )

            choice = response.choices[0]
            message = choice.message

            # Check if the model wants to call a tool
            if message.tool_calls:
                print(f"[WEB_SEARCH] AI requested {len(message.tool_calls)} tool call(s)")
                logger.info(f"AI requested {len(message.tool_calls)} tool call(s)")

                # Add the assistant's message with tool calls
                current_messages.append({
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        for tc in message.tool_calls
                    ]
                })

                # Execute each tool call
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        arguments = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        arguments = {}

                    logger.info(f"Executing tool '{tool_name}' with args: {arguments}")

                    # Execute the tool
                    tool_result = execute_tool(tool_name, arguments)

                    logger.info(f"Tool result: {tool_result[:200]}...")

                    # Add tool result
                    current_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result
                    })

                continue

            # No tool calls - final response
            return message.content or ""

        except Exception as e:
            logger.error(f"Error in tool-enabled AI response: {e}")
            raise

    # Max iterations reached
    logger.warning(f"Max tool iterations ({max_tool_iterations}) reached")
    return current_messages[-1].get("content", "") if current_messages else ""
