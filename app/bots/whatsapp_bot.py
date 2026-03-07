"""
WhatsApp Bot Runner
Runs the actual WhatsApp bot for a profile.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import queue
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import quote, unquote, urlparse

import httpx

logger = logging.getLogger(__name__)

# Get base directory for session storage (absolute path)
# This ensures consistent path resolution regardless of working directory
# Use the same session path for both frozen executable and script mode
if getattr(sys, 'frozen', False):
    # Running as frozen executable (PyInstaller)
    # Executable is at: <project>/dist/ChatHub/ChatHub.exe
    # Go up 3 levels to reach project root
    BASE_DIR = Path(sys.executable).resolve().parent.parent.parent
else:
    # Running as script
    # Script is at: <project>/app/bots/whatsapp_bot.py
    # Go up 3 levels to reach project root
    BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Both modes use the same session path: <project>/data/sessions
SESSIONS_DIR = BASE_DIR / "data" / "sessions"

# Default profile picture for AI Agent messages
AI_AGENT_PROFILE_PIC = "/static/images/ai-agent.svg"


def _is_browser_connected(page, context) -> bool:
    """
    Check if the browser/page is still connected and responsive.
    Returns True if browser is working, False if disconnected.
    """
    try:
        if page is None or context is None:
            return False
        # Try a simple operation that will fail if browser is disconnected
        # Checking if page is closed is lightweight
        if page.is_closed():
            return False
        # Try to get page title as a connectivity test
        page.title()
        return True
    except Exception:
        return False


def _media_url_to_path(file_url: str) -> Path:
    """
    Convert a media URL to an absolute file system path.
    Works correctly on all operating systems (Windows, macOS, Linux).

    Args:
        file_url: URL like '/media/bot_1/chat_abc/image.jpg'

    Returns:
        Absolute Path object to the file
    """
    from urllib.parse import unquote

    # Decode URL encoding (e.g., %20 -> space)
    decoded_url = unquote(file_url)

    # Remove the /media/ prefix and split into path components
    # URLs always use forward slashes regardless of OS
    if decoded_url.startswith('/media/'):
        relative_parts = decoded_url[7:].split('/')  # Skip '/media/'
    elif decoded_url.startswith('media/'):
        relative_parts = decoded_url[6:].split('/')  # Skip 'media/'
    else:
        relative_parts = decoded_url.split('/')

    # Build the path using pathlib (handles OS-specific separators)
    file_path = SESSIONS_DIR
    for part in relative_parts:
        if part:  # Skip empty parts
            file_path = file_path / part

    return file_path


# Global storage for Playwright pages (for manual message sending)
# Maps bot_profile_id -> page object
_bot_pages: Dict[int, Any] = {}
_bot_pages_lock = threading.Lock()

# Message queue for manual sending - the bot's main loop will pick these up
# Maps bot_profile_id -> Queue of (chat_id, chat_name, message, result_event, result_holder)
_outgoing_message_queues: Dict[int, queue.Queue] = {}
_outgoing_queues_lock = threading.Lock()

# File queue for manual file sending
# Maps bot_profile_id -> Queue of (chat_id, file_path, caption, file_type, result_event, result_holder)
_outgoing_file_queues: Dict[int, queue.Queue] = {}
_outgoing_file_queues_lock = threading.Lock()

# Create group queue
# Maps bot_profile_id -> Queue of (group_name, phone_numbers, result_event, result_holder)
_create_group_queues: Dict[int, queue.Queue] = {}
_create_group_queues_lock = threading.Lock()


def _get_outgoing_queue(bot_profile_id: int) -> queue.Queue:
    """Get or create the outgoing message queue for a bot."""
    with _outgoing_queues_lock:
        if bot_profile_id not in _outgoing_message_queues:
            _outgoing_message_queues[bot_profile_id] = queue.Queue()
        return _outgoing_message_queues[bot_profile_id]


def _get_outgoing_file_queue(bot_profile_id: int) -> queue.Queue:
    """Get or create the outgoing file queue for a bot."""
    with _outgoing_file_queues_lock:
        if bot_profile_id not in _outgoing_file_queues:
            _outgoing_file_queues[bot_profile_id] = queue.Queue()
        return _outgoing_file_queues[bot_profile_id]


def _get_create_group_queue(bot_profile_id: int) -> queue.Queue:
    """Get or create the create-group queue for a bot."""
    with _create_group_queues_lock:
        if bot_profile_id not in _create_group_queues:
            _create_group_queues[bot_profile_id] = queue.Queue()
        return _create_group_queues[bot_profile_id]


def cleanup_bot_queues(bot_profile_id: int) -> None:
    """
    Clean up queues for a bot when it's stopped.
    Call this from BotManager.remove_instance() to prevent memory leaks.
    """
    with _outgoing_queues_lock:
        if bot_profile_id in _outgoing_message_queues:
            del _outgoing_message_queues[bot_profile_id]
    with _outgoing_file_queues_lock:
        if bot_profile_id in _outgoing_file_queues:
            del _outgoing_file_queues[bot_profile_id]
    with _create_group_queues_lock:
        if bot_profile_id in _create_group_queues:
            del _create_group_queues[bot_profile_id]


def _is_valid_whatsapp_id(chat_id: str) -> bool:
    """
    Validate if a chat_id is a valid WhatsApp identifier.

    Valid formats:
    - Private chats: "971524906816@c.us" (phone number + @c.us)
    - Private chats (new): "34691155513450@lid" (linked ID + @lid)
    - Group chats: "120363425279786981@g.us" (group ID + @g.us)

    Returns:
        True if the chat_id is a valid WhatsApp identifier, False otherwise.
    """
    if not chat_id:
        return False
    return chat_id.endswith('@c.us') or chat_id.endswith('@g.us') or chat_id.endswith('@lid')


def _strip_markdown_formatting(text: str) -> str:
    """
    Strip markdown formatting from text to ensure clean output.
    Removes bold (**text** or __text__), italic (*text* or _text_),
    strikethrough (~~text~~), and code blocks.
    """
    if not text:
        return text

    # Remove bold: **text** or __text__
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)

    # Remove italic: *text* or _text_ (but not if it's part of a word like can't)
    # Be careful with single asterisks - only remove if surrounded by spaces or at boundaries
    text = re.sub(r'(?<![a-zA-Z])\*([^*\n]+)\*(?![a-zA-Z])', r'\1', text)
    text = re.sub(r'(?<![a-zA-Z])_([^_\n]+)_(?![a-zA-Z])', r'\1', text)

    # Remove strikethrough: ~~text~~
    text = re.sub(r'~~([^~]+)~~', r'\1', text)

    # Remove inline code: `text`
    text = re.sub(r'`([^`]+)`', r'\1', text)

    # Remove code blocks: ```text```
    text = re.sub(r'```[a-z]*\n?(.*?)```', r'\1', text, flags=re.DOTALL)

    return text


def _strip_sender_prefix(text: str) -> str:
    """
    Strip [Name]: prefix from AI response.
    The AI sometimes copies the conversation format [Name]: message.
    This removes such prefixes from the start of responses.
    """
    if not text:
        return text

    # Remove [Name]: or [Name] : prefix at the start of the response
    # Match pattern like [Ali]: or [Tony]: at the beginning
    text = re.sub(r'^\s*\[[^\]]+\]\s*:\s*', '', text)

    return text


async def send_whatsapp_message(bot_profile_id: int, chat_id: str, chat_name: str, message: str) -> bool:
    """
    Send a message via WhatsApp by adding to the bot's outgoing message queue.
    The bot's main loop will pick up and send the message.

    Args:
        bot_profile_id: The bot profile ID
        chat_id: The chat ID (phone number or group ID)
        chat_name: The chat name (for finding the chat)
        message: The message to send

    Returns:
        True if message was sent successfully, False otherwise
    """
    # Check if the bot page exists
    with _bot_pages_lock:
        page = _bot_pages.get(bot_profile_id)
        available_bots = list(_bot_pages.keys())

    if not page:
        logger.error(f"send_whatsapp_message: No page found for bot {bot_profile_id}. Available: {available_bots}")
        return False

    # Create result holder and event for synchronization
    result_event = threading.Event()
    result_holder = {'success': False, 'error': None}

    # Add to queue
    outgoing_queue = _get_outgoing_queue(bot_profile_id)
    outgoing_queue.put((chat_id, chat_name, message, result_event, result_holder))

    logger.info(f"send_whatsapp_message: Queued message for {chat_name}: {message[:50]}...")

    # Wait for result (with timeout) - run in thread pool to avoid blocking event loop
    # This keeps WhatsApp operations sequential while allowing FastAPI to handle other requests
    wait_result = await asyncio.to_thread(result_event.wait, timeout=30)

    if wait_result:
        if result_holder['success']:
            logger.info(f"send_whatsapp_message: Successfully sent to {chat_name}")
            return True
        else:
            error_msg = result_holder.get('error', 'Unknown error')
            logger.error(f"send_whatsapp_message: Failed - {error_msg}")
            return False
    else:
        logger.error(f"send_whatsapp_message: Timeout waiting for message to be sent")
        return False


async def send_whatsapp_file(bot_profile_id: int, chat_id: str, file_path: str, caption: str = "", file_type: str = "", chat_name: str = "") -> bool:
    """
    Send a file via WhatsApp by adding to the bot's outgoing file queue.
    The bot's main loop will pick up and send the file.

    Args:
        bot_profile_id: The bot profile ID
        chat_id: The chat ID (phone number or group ID)
        file_path: Path to the file to send
        caption: Optional caption for the file
        file_type: MIME type of the file
        chat_name: Display name of the chat (used to find chat in sidebar)

    Returns:
        True if file was sent successfully, False otherwise
    """
    # Check if the bot page exists
    with _bot_pages_lock:
        page = _bot_pages.get(bot_profile_id)

    if not page:
        logger.error(f"send_whatsapp_file: No page found for bot {bot_profile_id}")
        return False

    # Create result holder and event for synchronization
    result_event = threading.Event()
    result_holder = {'success': False, 'error': None}

    # Add to queue - include chat_name for finding the chat
    file_queue = _get_outgoing_file_queue(bot_profile_id)
    file_queue.put((chat_id, chat_name, file_path, caption, file_type, result_event, result_holder))

    logger.info(f"send_whatsapp_file: Queued file for chat {chat_name} ({chat_id}): {file_path}")

    # Wait for result (with timeout - longer for files) - run in thread pool to avoid blocking event loop
    wait_result = await asyncio.to_thread(result_event.wait, timeout=60)

    if wait_result:
        if result_holder['success']:
            logger.info(f"send_whatsapp_file: Successfully sent file to {chat_id}")
            return True
        else:
            logger.error(f"send_whatsapp_file: Failed - {result_holder.get('error', 'Unknown error')}")
            return False
    else:
        logger.error(f"send_whatsapp_file: Timeout waiting for file to be sent")
        return False


async def create_whatsapp_group(bot_profile_id: int, group_name: str, phone_numbers: list) -> dict:
    """
    Create a WhatsApp group by adding to the bot's create-group queue.
    The bot's main loop will pick up and create the group.

    Args:
        bot_profile_id: The bot profile ID
        group_name: Name for the new group
        phone_numbers: List of phone numbers to add as members

    Returns:
        dict with 'success' and optional 'error' keys
    """
    with _bot_pages_lock:
        page = _bot_pages.get(bot_profile_id)

    if not page:
        logger.error(f"create_whatsapp_group: No page found for bot {bot_profile_id}")
        return {'success': False, 'error': 'Bot page not found'}

    result_event = threading.Event()
    result_holder = {'success': False, 'error': None}

    create_group_queue = _get_create_group_queue(bot_profile_id)
    create_group_queue.put((group_name, phone_numbers, result_event, result_holder))

    logger.info(f"create_whatsapp_group: Queued group creation '{group_name}' with {len(phone_numbers)} members")

    wait_result = await asyncio.to_thread(result_event.wait, timeout=60)

    if wait_result:
        if result_holder['success']:
            logger.info(f"create_whatsapp_group: Successfully created group '{group_name}'")
            return {'success': True}
        else:
            error_msg = result_holder.get('error', 'Unknown error')
            logger.error(f"create_whatsapp_group: Failed - {error_msg}")
            return {'success': False, 'error': error_msg}
    else:
        logger.error(f"create_whatsapp_group: Timeout waiting for group creation")
        return {'success': False, 'error': 'Timeout waiting for group creation'}


def _process_create_group(page, bot_profile_id: int):
    """
    Process any pending create-group requests in the queue.
    Called from the bot's main message loop.
    """
    create_group_queue = _get_create_group_queue(bot_profile_id)

    try:
        group_name, phone_numbers, result_event, result_holder = create_group_queue.get_nowait()
    except queue.Empty:
        return

    try:
        logger.info(f"Bot {bot_profile_id}: Creating group '{group_name}' with members: {phone_numbers}")

        # Step 1: Click the "New chat" / compose button
        new_chat_btn = page.query_selector('[data-testid="chat-list-header-menu-new-chat"]') or \
                       page.query_selector('[aria-label="New chat"]') or \
                       page.query_selector('[data-testid="menu-bar-new-chat"]')
        if not new_chat_btn:
            raise Exception("Could not find 'New chat' button")
        new_chat_btn.click()
        time.sleep(1)

        # Step 2: Click "New group"
        new_group_btn = page.query_selector('[data-testid="btn-new-group"]') or \
                        page.query_selector('[aria-label="New group"]')
        if not new_group_btn:
            # Try finding by text
            elements = page.query_selector_all('div[role="button"], div[tabindex]')
            for el in elements:
                try:
                    text = el.inner_text()
                    if 'New group' in text or 'new group' in text.lower():
                        new_group_btn = el
                        break
                except:
                    pass
        if not new_group_btn:
            raise Exception("Could not find 'New group' button")
        new_group_btn.click()
        time.sleep(1.5)

        # Step 3: Add members by phone number
        for phone in phone_numbers:
            # Find the search/add participants input
            search_input = page.query_selector('[data-testid="search-input"]') or \
                          page.query_selector('input[placeholder*="contact"]') or \
                          page.query_selector('input[placeholder*="name"]') or \
                          page.query_selector('[contenteditable="true"][data-tab="3"]')
            if not search_input:
                # Try broader search for input in the add participants panel
                search_input = page.query_selector('[title="Search input textbox"]') or \
                              page.query_selector('div[contenteditable="true"]')
            if not search_input:
                raise Exception(f"Could not find search input to add member {phone}")

            search_input.click()
            time.sleep(0.3)
            search_input.fill(str(phone))
            time.sleep(2)

            # Click on the search result
            result = page.query_selector(f'span[title*="{phone}"]')
            if not result:
                # Try clicking the first search result
                result = page.query_selector('[data-testid="cell-frame-container"]') or \
                        page.query_selector('[role="row"]') or \
                        page.query_selector('[role="listitem"]')
            if result:
                result.click()
                time.sleep(0.5)
            else:
                logger.warning(f"Bot {bot_profile_id}: Could not find contact for phone {phone}")

        # Step 4: Click the forward/next arrow button
        next_btn = page.query_selector('[data-testid="arrow-forward"]') or \
                   page.query_selector('[aria-label="Next"]') or \
                   page.query_selector('span[data-icon="arrow-forward"]')
        if not next_btn:
            raise Exception("Could not find 'Next' button after adding members")
        next_btn.click()
        time.sleep(1.5)

        # Step 5: Enter group name
        group_name_input = page.query_selector('[data-testid="group-name-input"]') or \
                          page.query_selector('div[contenteditable="true"][role="textbox"]') or \
                          page.query_selector('div[contenteditable="true"]')
        if not group_name_input:
            raise Exception("Could not find group name input")
        group_name_input.click()
        time.sleep(0.3)
        group_name_input.fill(group_name)
        time.sleep(0.5)

        # Step 6: Click the create/checkmark button
        create_btn = page.query_selector('[data-testid="create-group-btn"]') or \
                    page.query_selector('[data-testid="arrow-forward"]') or \
                    page.query_selector('span[data-icon="checkmark-medium"]') or \
                    page.query_selector('[aria-label="Create group"]')
        if not create_btn:
            raise Exception("Could not find 'Create group' button")
        create_btn.click()
        time.sleep(2)

        result_holder['success'] = True
        logger.info(f"Bot {bot_profile_id}: Successfully created group '{group_name}'")

    except Exception as e:
        logger.error(f"Bot {bot_profile_id}: Error creating group: {e}")
        result_holder['error'] = str(e)
        # Try to close any open panels by pressing Escape
        try:
            page.keyboard.press('Escape')
            time.sleep(0.3)
            page.keyboard.press('Escape')
        except:
            pass
    finally:
        result_event.set()


def _process_outgoing_files(page, bot_profile_id: int):
    """
    Process any pending outgoing files in the queue.
    Called from the bot's main message loop.
    """
    file_queue = _get_outgoing_file_queue(bot_profile_id)

    # Process up to 2 files per cycle (files take longer)
    for _ in range(2):
        try:
            chat_id, chat_name, file_path, caption, file_type, result_event, result_holder = file_queue.get_nowait()
        except queue.Empty:
            break

        try:
            logger.info(f"Bot {bot_profile_id}: Sending file to {chat_name} ({chat_id})")

            # Extract phone number from chat_id for reliable identification
            phone_number = None
            is_group = '@g.us' in chat_id
            if chat_id and '@' in chat_id:
                phone_part = chat_id.split('@')[0]
                if phone_part.isdigit() or (phone_part and not is_group):
                    phone_number = phone_part

            chat_found = False
            chat_elem = None

            # Method 1: For contacts, try to find by phone number first
            if phone_number and not is_group:
                chat_elem = page.query_selector(f'[title="{phone_number}"]') or \
                           page.query_selector(f'span[title*="{phone_number}"]')
                if chat_elem:
                    chat_elem.click()
                    time.sleep(0.5)
                    chat_found = True

            # Method 2: Try by chat name
            if not chat_found and chat_name:
                chat_elem = page.query_selector(f'[title="{chat_name}"]') or \
                           page.query_selector(f'span[title*="{chat_name}"]')
                if chat_elem:
                    chat_elem.click()
                    time.sleep(0.5)
                    chat_found = True

            # Method 3: Try by data-id
            if not chat_found and '@' in chat_id:
                chat_elem = page.query_selector(f'[data-id="{chat_id}"]')
                if chat_elem:
                    chat_elem.click()
                    time.sleep(0.5)
                    chat_found = True

            # Method 4: Search for the chat
            if not chat_found:
                search_box = page.query_selector('[data-testid="chat-list-search"]') or \
                            page.query_selector('div[contenteditable="true"][data-tab="3"]')
                if search_box:
                    search_box.click()
                    time.sleep(0.3)
                    search_term = phone_number if (phone_number and not is_group) else (chat_name if chat_name else chat_id.split('@')[0])
                    search_box.fill(search_term)
                    time.sleep(1.5)

                    # For contacts: iterate through results and find correct one
                    if phone_number and not is_group:
                        # First try by chat name
                        if chat_name:
                            chat_elem = page.query_selector(f'[title="{chat_name}"]') or \
                                       page.query_selector(f'span[title*="{chat_name}"]')
                            if chat_elem:
                                chat_elem.click()
                                time.sleep(0.5)
                                chat_found = True

                        # Iterate through results and verify
                        if not chat_found:
                            selectors_to_try = [
                                '[role="row"]',
                                '[data-testid="cell-frame-container"]',
                                '#pane-side [role="listitem"]',
                            ]

                            all_results = []
                            for selector in selectors_to_try:
                                all_results = page.query_selector_all(selector)
                                if all_results:
                                    break

                            for result in all_results[:10]:
                                try:
                                    result_text = result.inner_text() if hasattr(result, 'inner_text') else ''

                                    # Stop at "Groups in common" section
                                    if 'Groups in common' in result_text:
                                        break

                                    # Skip section headers
                                    if result_text.strip() in ['Chats', 'Messages', 'Groups', 'Contacts']:
                                        continue

                                    result.click()
                                    time.sleep(0.8)

                                    # Check if this is a group chat
                                    is_group_chat = False

                                    if page.query_selector('[data-testid="conversation-header"] [data-icon="group"]') or \
                                       page.query_selector('header [data-icon="group"]'):
                                        is_group_chat = True

                                    if not is_group_chat:
                                        header_subtitle = page.query_selector('header span[title*=","]')
                                        if header_subtitle:
                                            subtitle = header_subtitle.get_attribute('title') or ''
                                            if ',' in subtitle and 'last seen' not in subtitle.lower():
                                                is_group_chat = True

                                    if not is_group_chat and '@g.us' in (page.url or ''):
                                        is_group_chat = True

                                    if is_group_chat:
                                        page.keyboard.press('Escape')
                                        time.sleep(0.5)
                                        search_box = page.query_selector('[data-testid="chat-list-search"]')
                                        if search_box:
                                            search_box.click()
                                            time.sleep(0.3)
                                        continue

                                    # Verify we're in a chat
                                    input_box = page.query_selector('[data-testid="conversation-compose-box-input"]') or \
                                               page.query_selector('footer [contenteditable="true"]')
                                    if input_box:
                                        chat_found = True
                                        break

                                except:
                                    continue

                    # For groups: try by name
                    if not chat_found and is_group and chat_name:
                        chat_elem = page.query_selector(f'[title="{chat_name}"]') or \
                                   page.query_selector(f'span[title*="{chat_name}"]')
                        if chat_elem:
                            chat_elem.click()
                            time.sleep(0.5)
                            chat_found = True

                    # Clear search
                    clear_btn = page.query_selector('[data-testid="x-alt"]')
                    if clear_btn:
                        clear_btn.click()
                        time.sleep(0.2)

            if not chat_found:
                logger.error(f"Bot {bot_profile_id}: Could not find chat {chat_name} ({chat_id})")
                result_holder['error'] = f"Could not find chat {chat_name} ({chat_id})"
                result_holder['success'] = False
                result_event.set()
                continue

            # Click attach button
            attach_btn = page.query_selector('[data-testid="attach-btn"]') or \
                        page.query_selector('[data-testid="clip"]') or \
                        page.query_selector('[data-icon="attach-menu-plus"]') or \
                        page.query_selector('[aria-label="Attach"]')

            if not attach_btn:
                logger.error(f"Bot {bot_profile_id}: Could not find attach button")
                result_holder['error'] = "Could not find attach button"
                result_holder['success'] = False
                result_event.set()
                continue

            attach_btn.click()
            time.sleep(1.0)

            # Determine which option to click based on file type
            is_media = file_type.startswith('image/') or file_type.startswith('video/')

            menu_item = None
            menu_item_selector = None

            if is_media:
                # Find Photos & Videos menu item
                photos_menu_selectors = [
                    'li:has-text("Photos & Videos")',
                    'li:has-text("Photos")',
                    '[data-testid="mi-attach-media"]',
                    'button:has-text("Photos")',
                    'div[role="button"]:has-text("Photos")',
                ]

                for selector in photos_menu_selectors:
                    try:
                        menu_item = page.query_selector(selector)
                        if menu_item:
                            menu_item_selector = selector
                            break
                    except Exception as e:
                        logger.debug(f"Bot {bot_profile_id}: Selector {selector} failed: {e}")
                        continue
            else:
                # Find Document menu item
                doc_menu_selectors = [
                    'li:has-text("Document")',
                    '[data-testid="mi-attach-document"]',
                    'button:has-text("Document")',
                    'div[role="button"]:has-text("Document")',
                ]

                for selector in doc_menu_selectors:
                    try:
                        menu_item = page.query_selector(selector)
                        if menu_item:
                            menu_item_selector = selector
                            break
                    except:
                        continue

            if not menu_item:
                logger.error(f"Bot {bot_profile_id}: Could not find menu item for {'media' if is_media else 'document'}")
                result_holder['error'] = f"Could not find {'Photos & Videos' if is_media else 'Document'} menu item"
                result_holder['success'] = False
                result_event.set()
                page.keyboard.press('Escape')
                continue

            try:
                # Set up file chooser listener and click menu item
                with page.expect_file_chooser(timeout=10000) as fc_info:
                    menu_item.click()

                file_chooser = fc_info.value
                file_chooser.set_files(file_path)

            except Exception as fc_error:
                logger.error(f"Bot {bot_profile_id}: File chooser failed: {fc_error}")
                # Fallback: try to find and use input directly
                time.sleep(0.5)
                all_inputs = page.query_selector_all('input[type="file"]')
                if all_inputs:
                    media_input = all_inputs[-1]
                    media_input.set_input_files(file_path)
                else:
                    logger.error(f"Bot {bot_profile_id}: No file input found for fallback")
                    result_holder['error'] = "File chooser failed and no fallback input"
                    result_holder['success'] = False
                    result_event.set()
                    page.keyboard.press('Escape')
                    continue
            time.sleep(2.5)  # Wait for file to load/preview

            # Verify we're in the media editor (preview screen)
            preview_selectors = [
                '[data-testid="media-editor"]',
                '[data-testid="image-preview"]',
                '[data-testid="media-canvas"]',
                '[data-testid="media-editor-popup"]',
                '[data-testid="doc-preview"]',
            ]
            preview_visible = None
            for sel in preview_selectors:
                preview_visible = page.query_selector(sel)
                if preview_visible:
                    break

            if not preview_visible:
                time.sleep(1.5)

            # Add caption if provided
            if caption:
                caption_selectors = [
                    '[data-testid="media-caption-input-container"] [contenteditable="true"]',
                    'div[data-testid="caption-input"] [contenteditable="true"]',
                    '[data-testid="media-editor"] [contenteditable="true"]',
                    '[data-testid="document-caption"] [contenteditable="true"]',
                    '[data-testid="doc-preview"] [contenteditable="true"]',
                    '.copyable-area [contenteditable="true"]',
                    '[aria-placeholder*="caption" i] ',
                    '[aria-placeholder*="Add a caption" i]',
                    'div[contenteditable="true"][data-tab="10"]',
                ]

                caption_input = None
                for selector in caption_selectors:
                    caption_input = page.query_selector(selector)
                    if caption_input:
                        break

                if caption_input:
                    caption_input.click()
                    time.sleep(0.2)
                    caption_input.fill(caption)
                    time.sleep(0.3)
                else:
                    page.keyboard.type(caption)
                    time.sleep(0.3)

            send_btn = None
            send_selectors = [
                '[data-testid="send"]',
                '[data-icon="send"]',
                'span[data-icon="send"]',
                '[aria-label="Send"]',
                'button[aria-label="Send"]',
                '[data-testid="media-editor"] [data-testid="send"]',
            ]

            for selector in send_selectors:
                send_btn = page.query_selector(selector)
                if send_btn:
                    break

            if not send_btn:
                logger.error(f"Bot {bot_profile_id}: Could not find send button for file")
                result_holder['error'] = "Could not find send button for file"
                result_holder['success'] = False
                result_event.set()
                page.keyboard.press('Escape')
                continue

            send_btn.click()
            time.sleep(0.5)
            page.keyboard.press('Enter')
            time.sleep(3)

            # Verify the preview screen closed
            preview_still_visible = page.query_selector('[data-testid="media-editor"]')
            if preview_still_visible:
                page.keyboard.press('Enter')
                time.sleep(2)

            logger.info(f"Bot {bot_profile_id}: Successfully sent file to {chat_id}")
            result_holder['success'] = True
            result_event.set()

            try:
                page.keyboard.press('Escape')
                time.sleep(0.3)
            except:
                pass

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Error sending file: {e}")
            result_holder['error'] = str(e)
            result_holder['success'] = False
            result_event.set()


def _process_outgoing_messages(page, bot_profile_id: int):
    """
    Process any pending outgoing messages in the queue.
    Called from the bot's main message loop.
    """
    outgoing_queue = _get_outgoing_queue(bot_profile_id)

    # Process up to 5 messages per cycle to avoid blocking
    for _ in range(5):
        try:
            # Non-blocking get
            chat_id, chat_name, message, result_event, result_holder = outgoing_queue.get_nowait()
        except queue.Empty:
            break

        try:
            logger.info(f"Bot {bot_profile_id}: Processing outgoing message for {chat_name} (chat_id: {chat_id})")

            # Extract phone number from chat_id for reliable identification
            phone_number = None
            is_group = '@g.us' in chat_id
            if chat_id and '@' in chat_id:
                phone_part = chat_id.split('@')[0]
                if phone_part.isdigit() or (phone_part and not is_group):
                    phone_number = phone_part

            chat_elem = None

            # Method 1: For contacts, try to find by phone number first
            if phone_number and not is_group:
                chat_elem = page.query_selector(f'[title="{phone_number}"]') or \
                           page.query_selector(f'span[title*="{phone_number}"]')

            # Method 2: Try by chat name (for groups or if phone not found)
            if not chat_elem and chat_name:
                chat_elem = page.query_selector(f'[title="{chat_name}"]') or \
                           page.query_selector(f'span[title*="{chat_name}"]')

            # Method 3: Use search to find the chat
            if not chat_elem:
                try:
                    search_term = phone_number if (phone_number and not is_group) else chat_name

                    search_box = page.query_selector('[data-testid="chat-list-search"]') or \
                                 page.query_selector('[contenteditable="true"][data-tab="3"]') or \
                                 page.query_selector('[aria-label*="Search"]')

                    if search_box:
                        search_box.click()
                        time.sleep(0.3)
                        search_box.fill(search_term)
                        time.sleep(1.5)

                        # For contacts: iterate through search results and find the correct one
                        if phone_number and not is_group:
                            # First try by chat name if we have it
                            if chat_name:
                                chat_elem = page.query_selector(f'[title="{chat_name}"]') or \
                                           page.query_selector(f'span[title*="{chat_name}"]')

                            # If not found by name, iterate through search results
                            if not chat_elem:
                                selectors_to_try = [
                                    '[role="row"]',
                                    '[data-testid="cell-frame-container"]',
                                    '#pane-side [role="listitem"]',
                                ]

                                all_results = []
                                for selector in selectors_to_try:
                                    try:
                                        all_results = page.query_selector_all(selector)
                                        if all_results:
                                            break
                                    except:
                                        pass

                                # Try each result and verify it's a contact (not a group)
                                for idx, result in enumerate(all_results[:10]):
                                    try:
                                        result_text = ""
                                        try:
                                            result_text = result.inner_text()
                                        except:
                                            pass

                                        # Stop at "Groups in common" section
                                        if 'Groups in common' in result_text:
                                            break

                                        # Skip section headers
                                        if result_text.strip() in ['Chats', 'Messages', 'Groups', 'Contacts']:
                                            continue

                                        result.click()
                                        time.sleep(0.8)

                                        # Check if this is a group chat
                                        is_group_chat = False

                                        # Check for group icon
                                        if page.query_selector('[data-testid="conversation-header"] [data-icon="group"]') or \
                                           page.query_selector('header [data-icon="group"]'):
                                            is_group_chat = True

                                        # Check header for multiple participants (groups show "Name1, Name2, You")
                                        if not is_group_chat:
                                            header_subtitle = page.query_selector('header span[title*=","]')
                                            if header_subtitle:
                                                subtitle_text = header_subtitle.get_attribute('title') or ''
                                                if ',' in subtitle_text and 'last seen' not in subtitle_text.lower():
                                                    is_group_chat = True

                                        # Check URL for group indicator
                                        if not is_group_chat:
                                            try:
                                                if '@g.us' in (page.url or ''):
                                                    is_group_chat = True
                                            except:
                                                pass

                                        if is_group_chat:
                                            page.keyboard.press('Escape')
                                            time.sleep(0.5)
                                            search_box = page.query_selector('[data-testid="chat-list-search"]')
                                            if search_box:
                                                search_box.click()
                                                time.sleep(0.3)
                                            continue

                                        # Verify we're in a chat
                                        input_box = page.query_selector('[data-testid="conversation-compose-box-input"]') or \
                                                   page.query_selector('footer [contenteditable="true"]')
                                        if input_box:
                                            chat_elem = result
                                            break
                                        else:
                                            page.keyboard.press('Escape')
                                            time.sleep(0.3)

                                    except:
                                        continue

                        # For groups: try by name
                        if not chat_elem and is_group:
                            chat_elem = page.query_selector(f'[title="{chat_name}"]') or \
                                       page.query_selector(f'span[title*="{chat_name}"]')

                except Exception as search_err:
                    logger.warning(f"Bot {bot_profile_id}: Search failed: {search_err}")

            if not chat_elem:
                error_msg = f"Could not find chat '{chat_name}' in sidebar or search (chat_id: {chat_id})"
                logger.warning(f"Bot {bot_profile_id}: {error_msg}")
                result_holder['error'] = error_msg
                result_holder['success'] = False
                result_event.set()
                continue

            # Click on the chat
            chat_elem.click()
            time.sleep(1)

            # Find message input box
            input_selectors = [
                '[data-testid="conversation-compose-box-input"]',
                '[contenteditable="true"][data-tab="10"]',
                'div[contenteditable="true"][role="textbox"]',
                'footer [contenteditable="true"]',
                '[aria-placeholder="Type a message"]',
                'div[title="Type a message"]'
            ]

            input_box = None
            for selector in input_selectors:
                try:
                    input_box = page.wait_for_selector(selector, timeout=3000)
                    if input_box:
                        break
                except:
                    continue

            if not input_box:
                result_holder['error'] = "Could not find message input box"
                result_holder['success'] = False
                result_event.set()
                continue

            # Type the message
            input_box.click()
            time.sleep(0.2)
            input_box.fill(message)
            time.sleep(0.3)

            # Click send button or press Enter
            send_selectors = [
                '[data-testid="send"]',
                '[data-icon="send"]',
                'button[aria-label="Send"]',
                'span[data-icon="send"]'
            ]

            send_button = None
            for selector in send_selectors:
                send_button = page.query_selector(selector)
                if send_button:
                    break

            if send_button:
                send_button.click()
            else:
                input_box.press('Enter')

            logger.info(f"Bot {bot_profile_id}: Sent manual message to {chat_name}: {message[:50]}...")

            time.sleep(1)

            # IMPORTANT: Click away from the chat to enable unread detection
            # Without this, new messages won't show unread indicators
            try:
                page.keyboard.press('Escape')
                time.sleep(0.3)
                # Click on the chat list header to deselect the chat
                chat_list_header = page.query_selector('[data-testid="chatlist-header"]') or \
                                   page.query_selector('[aria-label="Chat list"]')
                if chat_list_header:
                    chat_list_header.click()
                    time.sleep(0.2)
                logger.info(f"Bot {bot_profile_id}: Clicked away from chat after manual message to enable unread detection")
            except Exception as click_err:
                logger.warning(f"Bot {bot_profile_id}: Could not click away after manual message: {click_err}")

            result_holder['success'] = True
            result_event.set()

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Error sending outgoing message: {e}", exc_info=True)
            result_holder['error'] = str(e)
            result_holder['success'] = False
            result_event.set()


def _save_media(base64_data: str, media_type: str, bot_profile_id: int, chat_name: str, direction: str = 'received', original_filename: str = None) -> dict:
    """
    Save media from base64 data to the bot's session folder.
    Files are organized by conversation, type, and direction:
    data/sessions/bot_{id}/conversations/{chat_name}/images/received/

    Args:
        base64_data: Base64 encoded media data (data:mime/type;base64,...)
        media_type: Type of media ('image', 'video', 'document', 'sticker', 'audio')
        bot_profile_id: The bot profile ID for organizing files per bot
        chat_name: The conversation/contact name for organizing files per conversation
        direction: 'received' for incoming media, 'sent' for outgoing media
        original_filename: Original filename if available

    Returns:
        dict with file_url, file_type, file_name, file_size or None if failed
    """
    import base64
    import uuid
    from pathlib import Path

    try:
        # Parse base64 data
        if ',' in base64_data:
            header, data = base64_data.split(',', 1)
            # Extract mime type from header (data:image/png;base64)
            mime_type = header.split(':')[1].split(';')[0] if ':' in header else 'application/octet-stream'
        else:
            data = base64_data
            mime_type = 'application/octet-stream'

        # Decode base64
        file_bytes = base64.b64decode(data)
        file_size = len(file_bytes)

        # Determine file extension
        ext_map = {
            'image/jpeg': '.jpg',
            'image/jpg': '.jpg',
            'image/png': '.png',
            'image/gif': '.gif',
            'image/webp': '.webp',
            'video/mp4': '.mp4',
            'video/webm': '.webm',
            'video/3gpp': '.3gp',
            'audio/ogg': '.ogg',
            'audio/mpeg': '.mp3',
            'audio/mp4': '.m4a',
            'application/pdf': '.pdf',
            'application/msword': '.doc',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
            'application/vnd.ms-excel': '.xls',
        }
        extension = ext_map.get(mime_type, '.bin')

        # Generate unique filename
        unique_id = uuid.uuid4().hex[:12]
        if original_filename:
            # Clean filename: extract from 'Download "filename.ext"' format if present
            clean_name = original_filename
            download_match = re.match(r'Download\s+"([^"]+)"', original_filename)
            if download_match:
                clean_name = download_match.group(1)
            else:
                # Remove leading/trailing quotes and whitespace
                clean_name = re.sub(r'^["\'\\s]+|["\'\\s]+$', '', clean_name)
            # Keep original name but add unique prefix
            safe_name = re.sub(r'[^\w\-_\.]', '_', clean_name)
            filename = f"{unique_id}_{safe_name}"
        else:
            prefix = "incoming" if direction == 'received' else "outgoing"
            filename = f"{prefix}_{media_type}_{unique_id}{extension}"

        # Determine type subfolder based on mime type
        # Images and videos go to 'images/', documents go to 'documents/'
        if mime_type.startswith('image/') or mime_type.startswith('video/'):
            type_folder = 'images'
        elif mime_type.startswith('audio/'):
            type_folder = 'audio'
        else:
            # Documents: pdf, doc, docx, xls, xlsx, etc.
            type_folder = 'documents'

        # Sanitize chat_name for use as folder name
        safe_chat_name = re.sub(r'[^\w\-_ ]', '_', chat_name).strip()
        if not safe_chat_name:
            safe_chat_name = 'unknown'

        # Save to bot's session folder: data/sessions/bot_{id}/conversations/{chat_name}/{type_folder}/{direction}/
        base_dir = Path(__file__).resolve().parent.parent.parent
        bot_media_dir = base_dir / 'data' / 'sessions' / f'bot_{bot_profile_id}' / 'conversations' / safe_chat_name / type_folder / direction
        bot_media_dir.mkdir(parents=True, exist_ok=True)

        file_path = bot_media_dir / filename
        with open(file_path, 'wb') as f:
            f.write(file_bytes)

        # URL path for serving the file (URL-encode the chat_name)
        from urllib.parse import quote, unquote
        encoded_chat_name = quote(safe_chat_name, safe='')
        file_url = f"/media/bot_{bot_profile_id}/conversations/{encoded_chat_name}/{type_folder}/{direction}/{filename}"

        logger.info(f"Bot {bot_profile_id}: Saved {direction} media for '{chat_name}': {file_url} ({file_size} bytes, {mime_type})")

        # Extract page count for PDF documents
        file_pages = None
        if mime_type == 'application/pdf':
            try:
                import PyPDF2
                with open(file_path, 'rb') as pdf_file:
                    pdf_reader = PyPDF2.PdfReader(pdf_file)
                    file_pages = len(pdf_reader.pages)
                    logger.info(f"PDF has {file_pages} pages")
            except Exception as pdf_err:
                logger.debug(f"Could not extract PDF page count: {pdf_err}")

        return {
            'file_url': file_url,
            'file_type': mime_type,
            'file_name': original_filename or filename,
            'file_size': file_size,
            'file_pages': file_pages,
            'local_file_path': str(file_path)  # Store the actual local path for analysis
        }

    except Exception as e:
        logger.error(f"Error saving media: {e}", exc_info=True)
        return None


def _save_incoming_media(base64_data: str, media_type: str, bot_profile_id: int, chat_name: str, original_filename: str = None) -> dict:
    """Wrapper for saving incoming (received) media."""
    return _save_media(base64_data, media_type, bot_profile_id, chat_name, 'received', original_filename)


def _save_outgoing_media(base64_data: str, media_type: str, bot_profile_id: int, chat_name: str, original_filename: str = None) -> dict:
    """Wrapper for saving outgoing (sent) media."""
    return _save_media(base64_data, media_type, bot_profile_id, chat_name, 'sent', original_filename)


def _normalize_sender_id(sender_id: str) -> str:
    """
    Normalize WhatsApp sender ID by removing suffixes.

    WhatsApp uses different ID formats:
    - @c.us = personal chat
    - @g.us = group chat
    - @lid = linked account ID
    - @s.whatsapp.net = status

    Returns just the phone number/ID without suffix.
    """
    if not sender_id:
        return None
    # Remove all known suffixes
    normalized = sender_id
    for suffix in ['@lid', '@c.us', '@g.us', '@s.whatsapp.net']:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
            break
    return normalized


def _get_system_timezone() -> tuple:
    """
    Get the system's local timezone.

    Returns:
        Tuple of (timezone_id, offset_hours) or (None, None) if detection fails
        Example: ('America/New_York', -5) or ('Asia/Dubai', 4)
    """
    try:
        # Method 1: Try to get IANA timezone name using tzlocal or system info
        try:
            # Python 3.9+ has zoneinfo, try to get local timezone
            if hasattr(time, 'tzname'):
                # Get current offset in hours
                local_now = datetime.now()
                utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                offset_seconds = (local_now - utc_now).total_seconds()
                offset_hours = int(round(offset_seconds / 3600))

                # Try to get timezone name from environment or system
                import os
                tz_name = os.environ.get('TZ')

                if not tz_name:
                    # Try platform-specific methods
                    if sys.platform == 'win32':
                        # Windows: try to map from registry or use generic name
                        try:
                            import winreg
                            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation")
                            tz_name, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
                            winreg.CloseKey(key)
                            # Windows uses different names, try to map common ones
                            win_to_iana = {
                                "Pacific Standard Time": "America/Los_Angeles",
                                "Mountain Standard Time": "America/Denver",
                                "Central Standard Time": "America/Chicago",
                                "Eastern Standard Time": "America/New_York",
                                "GMT Standard Time": "Europe/London",
                                "Central European Standard Time": "Europe/Paris",
                                "W. Europe Standard Time": "Europe/Berlin",
                                "Romance Standard Time": "Europe/Paris",
                                "Arabian Standard Time": "Asia/Dubai",
                                "Arab Standard Time": "Asia/Riyadh",
                                "India Standard Time": "Asia/Kolkata",
                                "China Standard Time": "Asia/Shanghai",
                                "Singapore Standard Time": "Asia/Singapore",
                                "Tokyo Standard Time": "Asia/Tokyo",
                                "Korea Standard Time": "Asia/Seoul",
                                "AUS Eastern Standard Time": "Australia/Sydney",
                                "New Zealand Standard Time": "Pacific/Auckland",
                            }
                            tz_name = win_to_iana.get(tz_name, None)
                        except Exception:
                            pass
                    elif sys.platform == 'darwin':
                        # macOS: check multiple possible locations
                        try:
                            # Method 1: /etc/localtime symlink
                            localtime_path = Path('/etc/localtime')
                            if localtime_path.is_symlink():
                                tz_path = str(localtime_path.resolve())
                                # Extract timezone from path like /var/db/timezone/zoneinfo/America/New_York
                                for marker in ['/zoneinfo/', '/share/zoneinfo/']:
                                    if marker in tz_path:
                                        tz_name = tz_path.split(marker)[-1]
                                        break
                            # Method 2: Read from system preferences (if above fails)
                            if not tz_name:
                                import subprocess
                                result = subprocess.run(
                                    ['systemsetup', '-gettimezone'],
                                    capture_output=True, text=True, timeout=5
                                )
                                if result.returncode == 0:
                                    # Output: "Time Zone: America/New_York"
                                    output = result.stdout.strip()
                                    if 'Time Zone:' in output:
                                        tz_name = output.split('Time Zone:')[-1].strip()
                        except Exception:
                            pass
                    else:
                        # Linux/Unix: try /etc/timezone or /etc/localtime
                        try:
                            etc_timezone = Path('/etc/timezone')
                            etc_localtime = Path('/etc/localtime')

                            if etc_timezone.exists():
                                tz_name = etc_timezone.read_text().strip()
                            elif etc_localtime.exists():
                                tz_path = str(etc_localtime.resolve())
                                # Extract timezone from path like /usr/share/zoneinfo/America/New_York
                                for marker in ['/zoneinfo/', '/share/zoneinfo/']:
                                    if marker in tz_path:
                                        tz_name = tz_path.split(marker)[-1]
                                        break
                        except Exception:
                            pass

                if tz_name:
                    logger.info(f"Detected system timezone: {tz_name} (UTC{offset_hours:+d})")
                    return (tz_name, offset_hours)
                else:
                    # Fall back to offset-based timezone
                    logger.info(f"Detected system timezone offset: UTC{offset_hours:+d}")
                    return (None, offset_hours)

        except Exception as e:
            logger.debug(f"Could not get timezone name: {e}")

        # Fallback: just get the offset
        local_now = datetime.now()
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        offset_seconds = (local_now - utc_now).total_seconds()
        offset_hours = int(round(offset_seconds / 3600))
        logger.info(f"Detected system timezone offset: UTC{offset_hours:+d}")
        return (None, offset_hours)

    except Exception as e:
        logger.warning(f"Error detecting system timezone: {e}")
        return (None, None)


def _detect_timezone_from_ip(proxy_url: str = None, proxy_username: str = None, proxy_password: str = None) -> tuple:
    """
    Auto-detect timezone from IP using geolocation API.
    If proxy is provided, detects from proxy IP. Otherwise detects from local IP.

    Args:
        proxy_url: Optional proxy URL (e.g., 'http://proxy.example.com:8080')
        proxy_username: Optional proxy username
        proxy_password: Optional proxy password

    Returns:
        Tuple of (timezone_id, offset_hours) or (None, None) if detection fails
        Example: ('America/New_York', -5) or ('Asia/Dubai', 4)
    """
    import httpx

    try:
        # Build client options
        client_kwargs = {"timeout": 10.0}

        if proxy_url:
            # Build proxy URL with auth if provided
            if proxy_username and proxy_password:
                from urllib.parse import urlparse
                parsed = urlparse(proxy_url)
                proxy_with_auth = f"{parsed.scheme}://{proxy_username}:{proxy_password}@{parsed.netloc}{parsed.path}"
            else:
                proxy_with_auth = proxy_url
            client_kwargs["proxy"] = proxy_with_auth

        # Use ip-api.com (free, no API key needed, 45 requests/minute limit)
        with httpx.Client(**client_kwargs) as client:
            response = client.get('http://ip-api.com/json/?fields=status,timezone,offset')

            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'success':
                    timezone_id = data.get('timezone')  # e.g., 'America/New_York'
                    offset_seconds = data.get('offset', 0)  # Offset in seconds
                    offset_hours = offset_seconds // 3600

                    source = "proxy" if proxy_url else "local IP"
                    logger.info(f"Auto-detected timezone from {source}: {timezone_id} (UTC{offset_hours:+d})")
                    return (timezone_id, offset_hours)

            logger.warning(f"Failed to detect timezone from IP: {response.status_code}")
            return (None, None)

    except Exception as e:
        logger.warning(f"Error detecting timezone from IP: {e}")
        return (None, None)


def _detect_whatsapp_timezone_offset(whatsapp_time_str: str, whatsapp_date_str: str = None) -> int:
    """
    Detect the timezone offset WhatsApp is using by comparing with current UTC time.

    This should be called when receiving a NEW message (not historical) to detect
    the timezone offset WhatsApp uses for displaying timestamps.

    IMPORTANT: The message must be recently sent (within ~5 minutes of current time)
    for accurate detection. Historical messages will give incorrect offsets.

    Args:
        whatsapp_time_str: Time string from data-pre-plain-text, e.g., '7:13 AM'
        whatsapp_date_str: Date string from data-pre-plain-text, e.g., '1/20/2026' (optional)

    Returns:
        Offset in hours (e.g., -5 means UTC-5), or None if detection fails
    """
    current_utc = datetime.utcnow()

    # Parse WhatsApp time
    time_formats = ['%I:%M %p', '%I:%M%p', '%H:%M']
    whatsapp_time = None

    for fmt in time_formats:
        try:
            whatsapp_time = datetime.strptime(whatsapp_time_str.strip(), fmt).time()
            break
        except ValueError:
            continue

    if not whatsapp_time:
        logger.warning(f"Could not parse WhatsApp time '{whatsapp_time_str}' for timezone detection")
        return None

    # Determine the date to use
    if whatsapp_date_str:
        # Parse the date from WhatsApp
        date_formats = ['%m/%d/%Y', '%d/%m/%Y', '%m/%d/%y', '%d/%m/%y']
        whatsapp_date = None
        for fmt in date_formats:
            try:
                whatsapp_date = datetime.strptime(whatsapp_date_str.strip(), fmt).date()
                break
            except ValueError:
                continue
        if not whatsapp_date:
            whatsapp_date = current_utc.date()
    else:
        whatsapp_date = current_utc.date()

    # Create datetime with parsed date and time
    whatsapp_dt = datetime.combine(whatsapp_date, whatsapp_time)

    # Try different timezone offsets and find one where the message time is within 10 minutes of now
    # Valid offsets range from UTC-12 to UTC+14
    for candidate_offset in range(-12, 15):
        # Calculate what UTC time would be if this offset is correct
        # utc_time = whatsapp_time - offset (for negative offset like UTC-6, we add 6 hours)
        candidate_utc = whatsapp_dt - timedelta(hours=candidate_offset)

        # Check if this UTC time is within 10 minutes of current UTC
        time_diff = abs((candidate_utc - current_utc).total_seconds())
        if time_diff <= 600:  # 10 minutes = 600 seconds
            logger.info(f"Detected WhatsApp timezone offset: UTC{candidate_offset:+d} (WhatsApp: {whatsapp_time_str}, calculated UTC: {candidate_utc.strftime('%H:%M')}, current UTC: {current_utc.strftime('%H:%M')}, diff: {time_diff:.0f}s)")
            return candidate_offset

    # If no valid offset found within 10 minutes, the message might be historical
    # Fall back to the old calculation but log a warning
    diff = whatsapp_dt - current_utc
    diff_hours = diff.total_seconds() / 3600

    # Handle day boundary
    if diff_hours > 12:
        diff_hours -= 24
    elif diff_hours < -12:
        diff_hours += 24

    offset = round(diff_hours)

    logger.warning(f"Could not find timezone offset with message time close to now. Message might be historical. "
                   f"Falling back to calculated offset UTC{offset:+d} (WhatsApp: {whatsapp_time_str}, UTC: {current_utc.strftime('%H:%M')})")

    return None  # Return None to indicate we couldn't reliably detect the offset


def _convert_whatsapp_timestamp_to_utc(full_timestamp: str, timezone_offset: int) -> datetime:
    """
    Convert a WhatsApp timestamp to UTC using the detected timezone offset.

    Args:
        full_timestamp: Format "HH:MM AM/PM, M/D/YYYY" from data-pre-plain-text
        timezone_offset: Hours from UTC (e.g., -5 for UTC-5)

    Returns:
        datetime in UTC, or None if parsing fails
    """
    from datetime import datetime, timedelta

    if not full_timestamp:
        return None

    try:
        # Split into time and date parts
        parts = full_timestamp.split(', ')
        if len(parts) != 2:
            logger.warning(f"Invalid timestamp format: '{full_timestamp}'")
            return None

        time_str = parts[0].strip()
        date_str = parts[1].strip()

        # Parse date
        date_formats = ['%m/%d/%Y', '%d/%m/%Y', '%m/%d/%y', '%d/%m/%y']
        parsed_date = None
        for fmt in date_formats:
            try:
                parsed_date = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue

        if not parsed_date:
            logger.warning(f"Could not parse date '{date_str}'")
            return None

        # Parse time
        time_formats = ['%I:%M %p', '%I:%M%p', '%H:%M']
        parsed_time = None
        for fmt in time_formats:
            try:
                parsed_time = datetime.strptime(time_str, fmt).time()
                break
            except ValueError:
                continue

        if not parsed_time:
            logger.warning(f"Could not parse time '{time_str}'")
            return None

        # Combine date and time (this is in WhatsApp's timezone)
        whatsapp_dt = datetime.combine(parsed_date, parsed_time)

        # Convert to UTC by subtracting the offset
        # If offset is -5 (UTC-5), we ADD 5 hours to get UTC
        utc_dt = whatsapp_dt - timedelta(hours=timezone_offset)

        logger.debug(f"Converted '{full_timestamp}' (UTC{timezone_offset:+d}) -> {utc_dt} (UTC)")

        return utc_dt

    except Exception as e:
        logger.warning(f"Error converting timestamp '{full_timestamp}': {e}")
        return None


def _parse_whatsapp_timestamp(full_timestamp: str, fallback_time: str = None, timezone_offset: int = None) -> datetime:
    """
    Parse WhatsApp timestamp from data-pre-plain-text attribute and convert to UTC.

    Args:
        full_timestamp: Format "HH:MM, DD/MM/YYYY" or "HH:MM AM/PM, DD/MM/YYYY" or "HH:MM, M/D/YYYY"
        fallback_time: Just time string "HH:MM" or "HH:MM AM/PM" for fallback
        timezone_offset: WhatsApp's timezone offset in hours (e.g., -5 for UTC-5).
                         If provided, the result is converted to UTC.

    Returns:
        datetime object (in UTC if timezone_offset provided) or None if parsing fails
    """
    from datetime import datetime, timedelta

    # If timezone_offset is provided, use the dedicated conversion function
    if timezone_offset is not None and full_timestamp:
        return _convert_whatsapp_timestamp_to_utc(full_timestamp, timezone_offset)

    # Log at INFO level for easier debugging
    if full_timestamp:
        logger.info(f"_parse_whatsapp_timestamp: input='{full_timestamp}' (no timezone conversion)")

    if full_timestamp:
        try:
            # Split into time and date parts
            parts = full_timestamp.split(', ')
            if len(parts) == 2:
                time_str = parts[0].strip()
                date_str = parts[1].strip()
                logger.info(f"_parse_whatsapp_timestamp: time_str='{time_str}', date_str='{date_str}'")

                # Try different date formats
                # WhatsApp can use DD/MM/YYYY or M/D/YYYY depending on locale
                date_formats = ['%d/%m/%Y', '%m/%d/%Y', '%d/%m/%y', '%m/%d/%y']
                # Try 12-hour formats FIRST since WhatsApp typically uses AM/PM
                time_formats = ['%I:%M %p', '%I:%M%p', '%H:%M']

                parsed_date = None
                for date_fmt in date_formats:
                    try:
                        parsed_date = datetime.strptime(date_str, date_fmt).date()
                        logger.info(f"_parse_whatsapp_timestamp: date parsed with '{date_fmt}' -> {parsed_date}")
                        break
                    except ValueError:
                        continue

                if parsed_date:
                    for time_fmt in time_formats:
                        try:
                            parsed_time = datetime.strptime(time_str, time_fmt).time()
                            result = datetime.combine(parsed_date, parsed_time)
                            logger.info(f"_parse_whatsapp_timestamp: time parsed with '{time_fmt}' -> {parsed_time}, RESULT: {result}")
                            return result
                        except ValueError:
                            logger.debug(f"_parse_whatsapp_timestamp: '{time_str}' didn't match format '{time_fmt}'")
                            continue
                    logger.warning(f"_parse_whatsapp_timestamp: Could not parse time '{time_str}' with any format")
                else:
                    logger.warning(f"_parse_whatsapp_timestamp: Could not parse date '{date_str}' with any format")
        except Exception as e:
            logger.warning(f"_parse_whatsapp_timestamp: Error parsing '{full_timestamp}': {e}")

    # Fallback: parse just time with today's date
    if fallback_time:
        try:
            today = datetime.utcnow().date()
            time_formats = ['%H:%M', '%I:%M %p', '%I:%M%p']
            for time_fmt in time_formats:
                try:
                    parsed_time = datetime.strptime(fallback_time.strip(), time_fmt).time()
                    return datetime.combine(today, parsed_time)
                except ValueError:
                    continue
        except Exception:
            pass

    return None


def _analyze_image_with_ai(ai_provider, file_path: str, file_type: str, user_message: str = "") -> str:
    """
    Analyze an image using AI vision and return a text description.

    Args:
        ai_provider: AI provider instance (supports vision check)
        file_path: Path to the image file
        file_type: MIME type of the image
        user_message: Optional user's message accompanying the image

    Returns:
        Text description/analysis of the image
    """
    import base64
    from pathlib import Path

    try:
        if not ai_provider.supports_vision:
            return "[Image received - AI provider does not support image analysis]"

        path = Path(file_path)
        if not path.exists():
            logger.warning(f"Image file not found for analysis: {file_path}")
            return None

        # Read and encode the image
        with open(path, 'rb') as f:
            image_data = f.read()
        image_base64 = base64.b64encode(image_data).decode('utf-8')

        # Build the analysis prompt
        analysis_prompt = "Describe this image in 2-3 sentences. Focus on the key elements, context, and any text visible. Be concise but capture the essential details."
        if user_message:
            analysis_prompt = f"The user sent this image with the message: '{user_message}'. Describe what's in the image in 2-3 sentences, focusing on details relevant to their message."

        response = ai_provider.analyze_image(
            image_data=f"data:{file_type};base64,{image_base64}",
            prompt=analysis_prompt,
            detail="low",
            max_tokens=200
        )

        analysis = response.content
        logger.info(f"Image analysis completed: {analysis[:100]}...")
        return analysis

    except Exception as e:
        logger.error(f"Error analyzing image: {e}")
        return None


def _extract_document_text(file_path: str, file_type: str, max_chars: int = 5000) -> str:
    """
    Extract text content from various document formats.

    Supported formats:
    - PDF (.pdf)
    - Word (.docx, .doc)
    - Excel (.xlsx, .xls, .csv)
    - PowerPoint (.pptx)
    - Text files (.txt, .md, .json, .xml, .html, .css, .js, .py, etc.)

    Args:
        file_path: Path to the document file
        file_type: MIME type of the file
        max_chars: Maximum characters to extract (default 5000)

    Returns:
        Extracted text content or None if extraction fails
    """
    from pathlib import Path

    try:
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"Document file not found: {file_path}")
            return None

        file_ext = path.suffix.lower()
        text_content = None

        # PDF files
        if file_type == 'application/pdf' or file_ext == '.pdf':
            try:
                # Try pypdf first (successor to PyPDF2)
                from pypdf import PdfReader
                with open(file_path, 'rb') as f:
                    reader = PdfReader(f)
                    text_parts = []
                    for page in reader.pages[:10]:  # Limit to first 10 pages
                        text_parts.append(page.extract_text() or '')
                    text_content = '\n'.join(text_parts)
                logger.info(f"Extracted {len(text_content)} chars from PDF using pypdf")
            except ImportError:
                logger.warning("pypdf not installed, trying PyPDF2...")
                try:
                    import PyPDF2
                    with open(file_path, 'rb') as f:
                        reader = PyPDF2.PdfReader(f)
                        text_parts = []
                        for page in reader.pages[:10]:
                            text_parts.append(page.extract_text() or '')
                        text_content = '\n'.join(text_parts)
                    logger.info(f"Extracted {len(text_content)} chars from PDF using PyPDF2")
                except ImportError:
                    logger.warning("PyPDF2 not installed, trying pdfplumber...")
                    try:
                        import pdfplumber
                        with pdfplumber.open(file_path) as pdf:
                            text_parts = []
                            for page in pdf.pages[:10]:
                                text_parts.append(page.extract_text() or '')
                            text_content = '\n'.join(text_parts)
                        logger.info(f"Extracted {len(text_content)} chars from PDF using pdfplumber")
                    except ImportError:
                        logger.error("No PDF library available (install pypdf, PyPDF2, or pdfplumber)")
                        return None

        # Word documents (.docx)
        elif file_type in ['application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                           'application/msword'] or file_ext in ['.docx', '.doc']:
            try:
                from docx import Document
                doc = Document(file_path)
                text_parts = [para.text for para in doc.paragraphs]
                text_content = '\n'.join(text_parts)
                logger.info(f"Extracted {len(text_content)} chars from Word document")
            except ImportError:
                logger.error("python-docx not installed")
                return None

        # Excel files (.xlsx) - use openpyxl
        elif file_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' or file_ext == '.xlsx':
            try:
                import openpyxl
                wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
                text_parts = []
                for sheet_name in wb.sheetnames[:3]:  # Limit to first 3 sheets
                    sheet = wb[sheet_name]
                    text_parts.append(f"=== Sheet: {sheet_name} ===")
                    rows = list(sheet.iter_rows(max_row=50, values_only=True))  # Limit rows
                    for row in rows:
                        row_text = ' | '.join(str(cell) if cell else '' for cell in row)
                        if row_text.strip():
                            text_parts.append(row_text)
                text_content = '\n'.join(text_parts)
                logger.info(f"Extracted {len(text_content)} chars from Excel (.xlsx)")
            except ImportError:
                logger.error("openpyxl not installed")
                return None

        # Excel files (.xls) - use xlrd for old format
        elif file_type == 'application/vnd.ms-excel' or file_ext == '.xls':
            try:
                import xlrd
                wb = xlrd.open_workbook(file_path)
                text_parts = []
                for sheet_idx in range(min(3, wb.nsheets)):  # Limit to first 3 sheets
                    sheet = wb.sheet_by_index(sheet_idx)
                    text_parts.append(f"=== Sheet: {sheet.name} ===")
                    for row_idx in range(min(50, sheet.nrows)):  # Limit rows
                        row = [str(sheet.cell_value(row_idx, col_idx)) for col_idx in range(sheet.ncols)]
                        row_text = ' | '.join(row)
                        if row_text.strip():
                            text_parts.append(row_text)
                text_content = '\n'.join(text_parts)
                logger.info(f"Extracted {len(text_content)} chars from Excel (.xls)")
            except ImportError:
                logger.error("xlrd not installed for .xls files (pip install xlrd)")
                return None

        # CSV files
        elif file_type == 'text/csv' or file_ext == '.csv':
            import csv
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                reader = csv.reader(f)
                text_parts = []
                for i, row in enumerate(reader):
                    if i >= 100:  # Limit rows
                        text_parts.append("... (truncated)")
                        break
                    text_parts.append(' | '.join(row))
                text_content = '\n'.join(text_parts)
            logger.info(f"Extracted {len(text_content)} chars from CSV")

        # PowerPoint (.pptx)
        elif file_type == 'application/vnd.openxmlformats-officedocument.presentationml.presentation' or file_ext == '.pptx':
            try:
                from pptx import Presentation
                prs = Presentation(file_path)
                text_parts = []
                for i, slide in enumerate(prs.slides[:20]):  # Limit slides
                    text_parts.append(f"=== Slide {i+1} ===")
                    for shape in slide.shapes:
                        if hasattr(shape, 'text'):
                            text_parts.append(shape.text)
                text_content = '\n'.join(text_parts)
                logger.info(f"Extracted {len(text_content)} chars from PowerPoint")
            except ImportError:
                logger.error("python-pptx not installed")
                return None

        # Text-based files (txt, md, json, xml, html, code files, etc.)
        elif file_type.startswith('text/') or file_ext in ['.txt', '.md', '.json', '.xml', '.html',
                                                            '.css', '.js', '.py', '.java', '.cpp',
                                                            '.c', '.h', '.yaml', '.yml', '.ini',
                                                            '.conf', '.log', '.sql', '.sh', '.bat']:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text_content = f.read()
            logger.info(f"Read {len(text_content)} chars from text file")

        # JSON files
        elif file_type == 'application/json' or file_ext == '.json':
            import json
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            text_content = json.dumps(data, indent=2)
            logger.info(f"Parsed JSON with {len(text_content)} chars")

        else:
            logger.warning(f"Unsupported document type: {file_type} ({file_ext})")
            return None

        # Truncate if too long
        if text_content and len(text_content) > max_chars:
            text_content = text_content[:max_chars] + "\n... (content truncated)"

        return text_content.strip() if text_content else None

    except Exception as e:
        logger.error(f"Error extracting document text from {file_path}: {e}", exc_info=True)
        return None


def _analyze_document_with_ai(ai_provider, file_path: str, file_type: str, user_message: str = "") -> str:
    """
    Analyze a document using AI and return a summary.

    Args:
        ai_provider: AI provider instance
        file_path: Path to the document file
        file_type: MIME type of the document
        user_message: Optional user's message accompanying the document

    Returns:
        AI-generated summary/analysis of the document
    """
    try:
        logger.info(f"Document analysis starting: file_path={file_path}, file_type={file_type}")

        # Extract text from document
        text_content = _extract_document_text(file_path, file_type)

        if not text_content:
            logger.warning(f"Document analysis failed: No text could be extracted from {file_path}")
            return None

        logger.info(f"Document text extracted: {len(text_content)} characters")

        # Build the analysis prompt
        if user_message:
            analysis_prompt = f"""The user sent a document with the message: "{user_message}"

Document content:
{text_content}

Provide a concise summary (2-4 sentences) of this document, focusing on the key information relevant to the user's message."""
        else:
            analysis_prompt = f"""Summarize this document in 2-4 sentences. Focus on the main topic, key points, and any important data or conclusions.

Document content:
{text_content}"""

        response = ai_provider.chat_completion(
            messages=[{"role": "user", "content": analysis_prompt}],
            max_tokens=300,
            temperature=0.3
        )

        analysis = response.content
        logger.info(f"Document analysis completed: {analysis[:100]}...")
        return analysis

    except Exception as e:
        logger.error(f"Error analyzing document: {e}")
        return None


def _analyze_media_with_ai(ai_provider, file_path: str, file_type: str, user_message: str = "") -> str:
    """
    Analyze any media file (image, document, etc.) and return a description/summary.

    Routes to appropriate analyzer based on file type.

    Args:
        ai_provider: AI provider instance
        file_path: Path to the media file
        file_type: MIME type of the file
        user_message: Optional user's message accompanying the file

    Returns:
        AI-generated analysis of the media
    """
    if file_type.startswith('image/'):
        return _analyze_image_with_ai(ai_provider, file_path, file_type, user_message)
    else:
        return _analyze_document_with_ai(ai_provider, file_path, file_type, user_message)


def _build_openai_message(msg, sender_prefix: str = None, include_image: bool = False) -> dict:
    """
    Build an OpenAI-compatible message dict, with vision support for images.

    For history messages (include_image=False): uses stored media_analysis text
    For current message (include_image=True): includes actual base64 image

    Args:
        msg: Message object with content, role, file_url, file_type, sender_name, media_analysis
        sender_prefix: Optional sender name to prefix (for group chats)
        include_image: If True, include actual image; if False, use stored analysis

    Returns:
        OpenAI message dict
    """
    import base64
    from pathlib import Path

    content = msg.content or ""
    if sender_prefix and msg.role == "user":
        content = f"[{sender_prefix}]: {content}"

    # Check if message has an image attachment
    if msg.file_url and msg.file_type and msg.file_type.startswith('image/'):
        # Check if we have stored analysis and don't need to include actual image
        has_analysis = hasattr(msg, 'media_analysis') and msg.media_analysis

        if has_analysis and not include_image:
            # Use stored analysis instead of image (for history messages)
            analysis_text = f"{content}\n[Image: {msg.media_analysis}]" if content else f"[Image: {msg.media_analysis}]"
            logger.info(f"_build_openai_message: Using stored analysis for image")
            return {"role": msg.role, "content": analysis_text}

        # Include actual image (for current message or if no analysis stored)
        if include_image or not has_analysis:
            logger.info(f"_build_openai_message: Including actual image - file_url={msg.file_url}")
            try:
                file_path = _media_url_to_path(msg.file_url)

                if file_path.exists():
                    with open(file_path, 'rb') as f:
                        image_data = f.read()
                    image_base64 = base64.b64encode(image_data).decode('utf-8')

                    content_parts = []
                    text_content = content if content else "Please describe or respond to this image."
                    content_parts.append({"type": "text", "text": text_content})
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{msg.file_type};base64,{image_base64}",
                            "detail": "auto"
                        }
                    })

                    logger.info(f"_build_openai_message: Returning multi-part message with actual image")
                    return {"role": msg.role, "content": content_parts}
                else:
                    logger.warning(f"_build_openai_message: Image file NOT FOUND at: {file_path}")
            except Exception as e:
                logger.error(f"Error building image message: {e}")

    # Check if message has a document attachment (non-image file)
    # Handle both cases: file_url exists, OR file_url is None but we have file_type and file_name (couldn't download)
    elif msg.file_type and not msg.file_type.startswith('image/') and (msg.file_url or msg.file_name):
        has_analysis = hasattr(msg, 'media_analysis') and msg.media_analysis

        if has_analysis:
            # Use stored analysis for document
            doc_type = _get_document_type_label(msg.file_type)
            analysis_text = f"{content}\n[{doc_type}: {msg.media_analysis}]" if content else f"[{doc_type}: {msg.media_analysis}]"
            logger.info(f"_build_openai_message: Using stored analysis for document")
            return {"role": msg.role, "content": analysis_text}
        else:
            # No analysis stored - just mention the file was attached
            doc_type = _get_document_type_label(msg.file_type)
            download_note = " (could not download)" if not msg.file_url else ""
            file_note = f"{content}\n[{doc_type} attached: {msg.file_name or 'document'}{download_note}]" if content else f"[{doc_type} attached: {msg.file_name or 'document'}{download_note}]"
            return {"role": msg.role, "content": file_note}

    # Default: return simple text message
    return {"role": msg.role, "content": content}


def _get_document_type_label(file_type: str) -> str:
    """Get a human-readable label for document type."""
    type_labels = {
        'application/pdf': 'PDF Document',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'Word Document',
        'application/msword': 'Word Document',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'Excel Spreadsheet',
        'application/vnd.ms-excel': 'Excel Spreadsheet',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'PowerPoint Presentation',
        'text/csv': 'CSV File',
        'text/plain': 'Text File',
        'application/json': 'JSON File',
        'text/html': 'HTML File',
        'application/xml': 'XML File',
    }
    return type_labels.get(file_type, 'Document')


async def run_whatsapp_bot(instance):
    """
    Run WhatsApp bot for a given instance.
    On Windows, runs Playwright synchronously in a separate thread.

    Args:
        instance: BotInstance from manager
    """
    config = instance.config
    bot_profile_id = instance.bot_profile_id

    # Store reference to main event loop for callbacks
    main_loop = asyncio.get_event_loop()

    # Wrapper to post async callbacks back to main loop
    def notify_status_sync(status):
        print(f"Bot {bot_profile_id}: notify_status_sync called with: {status}", flush=True)
        logger.info(f"Bot {bot_profile_id}: notify_status_sync called with: {status}")
        try:
            future = asyncio.run_coroutine_threadsafe(
                instance.notify_status(status), main_loop
            )
            future.result(timeout=5)
            print(f"Bot {bot_profile_id}: notify_status_sync completed", flush=True)
            logger.info(f"Bot {bot_profile_id}: notify_status_sync completed")
        except Exception as e:
            print(f"Bot {bot_profile_id}: Error notifying status: {e}", flush=True)
            logger.error(f"Bot {bot_profile_id}: Error notifying status: {e}", exc_info=True)

    def notify_qr_sync(qr_code):
        print(f"Bot {bot_profile_id}: notify_qr_sync called, QR length: {len(qr_code)}", flush=True)
        logger.info(f"Bot {bot_profile_id}: notify_qr_sync called, QR length: {len(qr_code)}")
        try:
            future = asyncio.run_coroutine_threadsafe(
                instance.notify_qr(qr_code), main_loop
            )
            future.result(timeout=5)
            print(f"Bot {bot_profile_id}: notify_qr_sync completed successfully", flush=True)
            logger.info(f"Bot {bot_profile_id}: notify_qr_sync completed successfully")
        except Exception as e:
            print(f"Bot {bot_profile_id}: Error notifying QR: {e}", flush=True)
            logger.error(f"Bot {bot_profile_id}: Error notifying QR: {e}", exc_info=True)

    if True:  # Force sync mode - async version is incomplete (missing message loop)
        # Run Playwright synchronously in a separate thread
        def run_in_thread():
            print(f"Bot {bot_profile_id}: Thread started", flush=True)
            logger.info(f"Bot {bot_profile_id}: Thread started")
            try:
                _run_whatsapp_bot_sync(
                    instance, config, bot_profile_id, notify_status_sync, notify_qr_sync
                )
            except Exception as e:
                print(f"Bot {bot_profile_id}: Thread error - {e}", flush=True)
                logger.error(f"Bot {bot_profile_id}: Thread error - {e}")
                instance.error = str(e)

        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()

        # Wait for thread to complete or for cancellation
        while thread.is_alive() and instance.is_running:
            await asyncio.sleep(1)

        if thread.is_alive():
            instance.is_running = False
    else:
        # On non-Windows, run async
        await _run_whatsapp_bot_async(
            instance, config, bot_profile_id,
            lambda s: asyncio.create_task(instance.notify_status(s)),
            lambda q: asyncio.create_task(instance.notify_qr(q))
        )


def _run_whatsapp_bot_sync(instance, config, bot_profile_id, notify_status, notify_qr):
    """
    Synchronous implementation using Playwright sync API (for Windows).
    """
    from playwright.sync_api import sync_playwright
    from app.database import get_db_session, BotProfile, Conversation, Message, ActivityLog
    from app.auth.utils import decrypt_string
    from app.ai.factory import get_ai_provider
    from datetime import datetime, timedelta  # Import at function start to avoid scoping issues
    import time

    playwright = None
    context = None  # Persistent context (replaces browser)

    try:
        time.sleep(1)  # Wait for WebSocket
        print(f"Bot {bot_profile_id}: Starting...", flush=True)
        notify_status({"status": "starting", "message": "Initializing bot..."})
        logger.info(f"Bot {bot_profile_id}: Starting...")

        # Handle both decrypted (from auto-recovery) and encrypted (from routes) API key
        if 'api_key' in config:
            api_key = config['api_key']
        else:
            api_key = decrypt_string(config['api_key_encrypted'])
        ai_provider = get_ai_provider(
            config.get('ai_provider', 'openai'),
            api_key,
            model=config.get('model')
        )

        # Set AI cost tracking context for this bot
        try:
            from app.ai.cost_tracker import usage_context
            with get_db_session() as ctx_db:
                bot_profile = ctx_db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
                if bot_profile:
                    usage_context.set(
                        user_id=bot_profile.user_id,
                        bot_id=bot_profile_id,
                        source='bot_chat'
                    )
        except Exception as ctx_err:
            logger.debug(f"Bot {bot_profile_id}: Could not set usage context: {ctx_err}")

        print(f"Bot {bot_profile_id}: AI provider ({config.get('ai_provider', 'openai')}) initialized, launching browser...", flush=True)
        notify_status({"status": "launching", "message": "Launching browser..."})

        playwright = sync_playwright().start()
        print(f"Bot {bot_profile_id}: Playwright started", flush=True)

        # Get headless setting from config (default to False)
        headless_mode = bool(config.get('headless', False))
        print(f"Bot {bot_profile_id}: Launching browser with headless={headless_mode}", flush=True)
        logger.info(f"Bot {bot_profile_id}: Launching browser with headless={headless_mode}")

        # Create persistent session directory for this bot (use absolute path)
        # This stores ALL browser data (cookies, localStorage, IndexedDB, cache)
        # WhatsApp session will persist across bot restarts
        session_path = str(SESSIONS_DIR / f"bot_{bot_profile_id}")

        logger.info(f"Bot {bot_profile_id}: Using ABSOLUTE session path: {session_path}")

        # Check if this is a fresh session or existing one
        # Only consider it existing if it has browser data (not just our 'conversations' folder)
        browser_data_markers = ['Default', 'Local State', 'First Run']
        session_exists = os.path.exists(session_path) and any(
            os.path.exists(os.path.join(session_path, marker)) for marker in browser_data_markers
        )
        if session_exists:
            logger.info(f"Bot {bot_profile_id}: Found EXISTING browser session at: {session_path} - will attempt auto-login")
        else:
            logger.info(f"Bot {bot_profile_id}: Creating FRESH browser session at: {session_path} - will show QR code")

        os.makedirs(session_path, exist_ok=True)

        # Determine browser timezone:
        # 1. If proxy enabled, auto-detect from proxy IP
        # 2. If no proxy, use system timezone or detect from local IP
        # 3. If manual setting provided, use it
        # 4. Fall back to UTC (simplest, no conversion needed)
        proxy_enabled = config.get('proxy_enabled', False)
        proxy_url = config.get('proxy_url')
        proxy_username = None
        proxy_password = None
        detected_timezone = None
        detected_offset = None

        if proxy_enabled and proxy_url:
            # Decrypt proxy credentials if provided
            if config.get('proxy_username'):
                proxy_username = decrypt_string(config['proxy_username'])
            if config.get('proxy_password'):
                proxy_password = decrypt_string(config['proxy_password'])

            # Auto-detect timezone from proxy IP
            logger.info(f"Bot {bot_profile_id}: Detecting timezone from proxy: {proxy_url}")
            detected_timezone, detected_offset = _detect_timezone_from_ip(
                proxy_url, proxy_username, proxy_password
            )
        else:
            # No proxy - detect from system timezone or local IP
            logger.info(f"Bot {bot_profile_id}: No proxy configured, detecting system timezone...")
            detected_timezone, detected_offset = _get_system_timezone()

            # If we couldn't get timezone name from system, try IP geolocation
            if not detected_timezone and detected_offset is None:
                logger.info(f"Bot {bot_profile_id}: Falling back to IP geolocation...")
                detected_timezone, detected_offset = _detect_timezone_from_ip()

        # Determine final timezone to use
        if detected_timezone:
            browser_timezone = detected_timezone
            logger.info(f"Bot {bot_profile_id}: Using auto-detected timezone: {browser_timezone} (UTC{detected_offset:+d})")
        elif detected_offset is not None:
            # We have offset but no timezone name - construct Etc/GMT timezone
            # Note: Etc/GMT+X means UTC-X (counterintuitive but correct)
            if detected_offset == 0:
                browser_timezone = 'UTC'
            elif detected_offset > 0:
                browser_timezone = f'Etc/GMT-{detected_offset}'  # Etc/GMT-4 = UTC+4
            else:
                browser_timezone = f'Etc/GMT+{abs(detected_offset)}'  # Etc/GMT+5 = UTC-5
            logger.info(f"Bot {bot_profile_id}: Using offset-based timezone: {browser_timezone} (UTC{detected_offset:+d})")
        elif config.get('browser_timezone'):
            browser_timezone = config['browser_timezone']
            logger.info(f"Bot {bot_profile_id}: Using configured timezone: {browser_timezone}")
        else:
            browser_timezone = 'UTC'
            detected_offset = 0
            logger.info(f"Bot {bot_profile_id}: Using default timezone: UTC")

        # Build persistent context options
        # Extra args for headless mode to avoid detection
        browser_args = [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-blink-features=AutomationControlled'
        ]
        if headless_mode:
            browser_args.extend([
                '--disable-gpu',
                '--disable-dev-shm-usage',
                '--disable-software-rasterizer',
                '--window-size=1280,800'
            ])
        context_options = {
            "headless": headless_mode,
            "args": browser_args,
            "viewport": {'width': 1280, 'height': 800},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "locale": 'en-US',
            "timezone_id": browser_timezone  # WhatsApp timestamps will be in this timezone
        }

        # Add proxy settings if enabled
        if proxy_enabled and proxy_url:
            proxy_config = {"server": proxy_url}
            if proxy_username:
                proxy_config["username"] = proxy_username
            if proxy_password:
                proxy_config["password"] = proxy_password
            context_options["proxy"] = proxy_config
            logger.info(f"Bot {bot_profile_id}: Using proxy: {proxy_url}")

        # Use persistent context - automatically saves/restores ALL browser data
        # This includes cookies, localStorage, IndexedDB - everything WhatsApp needs
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=session_path,
            **context_options
        )

        context.add_init_script('''
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        ''')

        # Get the default page or create one
        if context.pages:
            page = context.pages[0]
        else:
            page = context.new_page()
        print(f"Bot {bot_profile_id}: Browser page ready", flush=True)

        # Mark browser as connected and record start time for auto-restart
        instance.browser_connected = True
        instance.browser_started_at = datetime.now()

        # Minimize the browser window to avoid covering the screen
        # Uses CDP (Chrome DevTools Protocol) to set window state
        try:
            cdp = context.new_cdp_session(page)
            # Get the window ID first
            window_info = cdp.send("Browser.getWindowForTarget")
            window_id = window_info.get("windowId")
            if window_id:
                # Minimize the window
                cdp.send("Browser.setWindowBounds", {
                    "windowId": window_id,
                    "bounds": {"windowState": "minimized"}
                })
                print(f"Bot {bot_profile_id}: Browser window minimized", flush=True)
        except Exception as e:
            # Non-fatal - browser will just remain visible
            print(f"Bot {bot_profile_id}: Could not minimize browser window: {e}", flush=True)

        print(f"Bot {bot_profile_id}: Loading WhatsApp Web...", flush=True)
        notify_status({"status": "loading", "message": "Loading WhatsApp Web..."})
        page.goto("https://web.whatsapp.com", wait_until="networkidle")
        print(f"Bot {bot_profile_id}: WhatsApp Web loaded", flush=True)

        notify_status({"status": "waiting_qr", "message": "Waiting for QR code..."})
        print(f"Bot {bot_profile_id}: Entering QR detection loop...", flush=True)
        logger.info(f"Bot {bot_profile_id}: Entering QR detection loop...")
        logger.info(f"Bot {bot_profile_id}: WhatsApp Web loaded, waiting for QR code...")

        authenticated = False
        last_qr_data = None  # Fingerprint for change detection
        last_sent_qr = None  # Track what we've actually sent
        for i in range(120):
            if not instance.is_running:
                logger.info(f"Bot {bot_profile_id}: Stopped by user")
                return

            # Check multiple selectors for authentication (supports WhatsApp Web and Business)
            auth_selectors = [
                '[data-testid="chat-list"]',
                '[data-testid="chatlist-header"]',
                '[data-testid="default-user"]',
                '[aria-label="Chat list"]',
                'div[data-tab="3"]',  # Chat tab
                '[data-testid="side"]'  # Side panel with chats
            ]

            for selector in auth_selectors:
                element = page.query_selector(selector)
                if element:
                    logger.info(f"Bot {bot_profile_id}: Authentication detected via selector: {selector}")
                    authenticated = True
                    instance.whatsapp_connected = True
                    notify_status({"connected": True, "whatsapp_connected": True, "message": "WhatsApp connected successfully!"})
                    break

            if authenticated:
                break

            # Also check page title - if no QR canvas and title suggests logged in
            page_title = page.title()
            has_canvas = page.query_selector('canvas') is not None

            # If no QR canvas and title contains WhatsApp (not just "WhatsApp"), likely logged in
            if not has_canvas and 'WhatsApp' in page_title and page_title != 'WhatsApp':
                # Additional check: look for any chat-related element
                chat_indicator = page.query_selector('[data-testid="conversation-panel-wrapper"], [data-testid="pane-side"]')
                if chat_indicator:
                    logger.info(f"Bot {bot_profile_id}: Authentication detected via title and chat panel: {page_title}")
                    authenticated = True
                    instance.whatsapp_connected = True
                    notify_status({"connected": True, "whatsapp_connected": True, "message": "WhatsApp connected successfully!"})
                    break

            # Get QR code from page - capture every second to ensure we don't miss changes
            qr_data = None
            if i >= 3:  # Wait at least 3 seconds for page to fully load
                # Debug: Show what QR-related elements exist on the page (only once)
                if i == 3:
                    qr_elements = page.evaluate('''
                        () => {
                            const canvases = Array.from(document.querySelectorAll('canvas')).map(c => ({
                                width: c.width,
                                height: c.height
                            }));
                            return { canvases };
                        }
                    ''')
                    print(f"Bot {bot_profile_id}: QR elements on page: {qr_elements}", flush=True)

                # Get QR code via canvas toDataURL (doesn't cause flicker)
                qr_data = page.evaluate('''
                    () => {
                        const canvas = document.querySelector('canvas');
                        if (!canvas) return null;
                        const w = canvas.width, h = canvas.height;
                        if (w < 200 || w > 350 || h < 200 || h > 350) return null;
                        try {
                            return canvas.toDataURL('image/png');
                        } catch(e) {
                            return null;
                        }
                    }
                ''')

            # Send QR if valid and different from last sent
            if qr_data and qr_data.startswith('data:image') and len(qr_data) > 5000:
                if qr_data != last_sent_qr:
                    print(f"Bot {bot_profile_id}: New QR found, len={len(qr_data)}", flush=True)
                    logger.info(f"Bot {bot_profile_id}: New QR found, len={len(qr_data)}")
                    print(f"Bot {bot_profile_id}: Valid QR code found! Length: {len(qr_data)}", flush=True)
                    logger.info(f"Bot {bot_profile_id}: Valid QR code found! Length: {len(qr_data)}")
                    last_sent_qr = qr_data
                    instance.qr_code = qr_data
                    notify_qr(qr_data)
                    print(f"Bot {bot_profile_id}: QR sent to WebSocket", flush=True)
                    logger.info(f"Bot {bot_profile_id}: QR sent to WebSocket")
            elif i % 10 == 0:
                qr_len = len(qr_data) if qr_data else 0
                print(f"Bot {bot_profile_id}: Waiting for QR... canvas={has_canvas}, qr_len={qr_len}", flush=True)
                logger.info(f"Bot {bot_profile_id}: Waiting for QR... canvas={has_canvas}, qr_len={qr_len}")

            if i > 0 and i % 10 == 0:
                logger.info(f"Bot {bot_profile_id}: Still waiting for auth... ({i}s)")

            time.sleep(1)

        if not authenticated:
            instance.error = "Authentication timeout"
            logger.error(f"Bot {bot_profile_id}: Authentication timeout")
            return

        # Session is automatically saved by persistent context - no manual save needed
        logger.info(f"Bot {bot_profile_id}: Authenticated! Session persisted automatically.")

        # Store page reference for manual message sending
        with _bot_pages_lock:
            _bot_pages[bot_profile_id] = page
            logger.info(f"Bot {bot_profile_id}: Stored page reference for manual messaging")

        # Extract WhatsApp account info from the page
        account_info = {}
        try:
            # Detect if this is a Business or Personal account
            page_title = page.title() or ''
            is_business = 'Business' in page_title
            account_info['account_type'] = 'business' if is_business else 'personal'
            logger.info(f"Bot {bot_profile_id}: Account type: {'Business' if is_business else 'Personal'}")

            # Get phone number from localStorage
            phone_info = page.evaluate('''() => {
                try {
                    const waData = localStorage.getItem('last-wid-md');
                    if (waData) {
                        const parsed = JSON.parse(waData);
                        let phone = parsed.split('@')[0] || null;
                        if (phone && phone.includes(':')) {
                            phone = phone.split(':')[0];
                        }
                        return phone;
                    }
                } catch (e) {}
                return null;
            }''')
            if phone_info:
                account_info['phone'] = phone_info

            # Extract name from Profile section by clicking on the Profile navbar button
            try:
                # Press Escape first to reset any open panels
                page.keyboard.press('Escape')
                page.wait_for_timeout(500)

                # Click the Profile button in the navbar (aria-label="Profile", data-navbar-item="true")
                # This works for both Business and Personal WhatsApp accounts
                profile_clicked = page.evaluate('''() => {
                    // Look for the Profile button in the navbar
                    const profileBtn = document.querySelector('button[aria-label="Profile"][data-navbar-item="true"]');
                    if (profileBtn) {
                        profileBtn.click();
                        return {result: 'navbar_profile', hasImg: !!profileBtn.querySelector('img')};
                    }

                    // Fallback: try aria-label="Profile" without data-navbar-item
                    const profileBtnAlt = document.querySelector('button[aria-label="Profile"]');
                    if (profileBtnAlt) {
                        profileBtnAlt.click();
                        return {result: 'aria_profile', hasImg: !!profileBtnAlt.querySelector('img')};
                    }

                    return {result: null};
                }''')

                click_result = profile_clicked.get('result') if isinstance(profile_clicked, dict) else profile_clicked
                print(f"Bot {bot_profile_id}: Profile click result: {click_result}", flush=True)
                logger.info(f"Bot {bot_profile_id}: Profile click result: {click_result}")

                # Wait for the profile panel to open
                page.wait_for_timeout(1500)

                # Now extract the name and profile pic from profile screen
                # This handles both Personal and Business WhatsApp profiles
                profile_data = page.evaluate(r'''() => {
                    const result = { debug: {} };

                    // Check if profile panel is open by looking for "Profile" header or close button
                    const headers = document.querySelectorAll('h2, header span, [class*="header"] span, header');
                    let profilePanelOpen = false;
                    for (const h of headers) {
                        const text = h.textContent?.trim() || '';
                        if (text === 'Profile' || text.includes('Business profile') || text.includes('Edit profile')) {
                            profilePanelOpen = true;
                            break;
                        }
                    }
                    // Also check for close button which indicates a panel is open
                    if (!profilePanelOpen) {
                        const closeBtn = document.querySelector('[data-icon="close"], [data-icon="close-refreshed"], [aria-label="Close"]');
                        if (closeBtn) {
                            const rect = closeBtn.getBoundingClientRect();
                            // Close button should be in a slide-out panel (right side or large left offset)
                            if (rect.left > 200 || rect.right > 300) {
                                profilePanelOpen = true;
                            }
                        }
                    }
                    result.debug.profilePanelOpen = profilePanelOpen;

                    // Check if this is a Business profile by looking for "Business Information" section
                    const pageText = document.body.innerText || '';
                    const isBusiness = pageText.includes('Business Information') || pageText.includes('Business name') || pageText.includes('Business profile');
                    result.is_business = isBusiness;
                    result.debug.isBusiness = isBusiness;
                    result.debug.hasProfileText = pageText.includes('Profile');
                    result.debug.hasNameText = pageText.includes('Name');

                    if (isBusiness) {
                        // ===== BUSINESS WHATSAPP PROFILE =====
                        // Business name: Look for "Business name" label and get the value span
                        const allSpans = document.querySelectorAll('span');
                        let foundBusinessName = false;
                        for (const span of allSpans) {
                            const text = span.textContent?.trim();
                            if (text === 'Business name') {
                                foundBusinessName = true;
                                continue;
                            }
                            if (foundBusinessName && text && text.length > 0 && text.length < 100) {
                                const lower = text.toLowerCase();
                                // Skip labels and empty values
                                if (!['business name', 'description', 'address', 'category', 'edit'].includes(lower)) {
                                    result.name = text;
                                    break;
                                }
                            }
                        }

                        // Business phone: Look for call-refreshed icon then get phone number
                        const callIcon = document.querySelector('[data-icon="call-refreshed"]');
                        if (callIcon) {
                            // Navigate up to find the container, then find the phone span
                            let container = callIcon.closest('div[class*="x1c4vz4f"]');
                            if (container) {
                                // Look for span with phone number pattern
                                const spans = container.querySelectorAll('span');
                                for (const span of spans) {
                                    const text = span.textContent?.trim();
                                    // Phone numbers typically start with + and contain digits
                                    if (text && /^\+?[\d\s\-()]+$/.test(text.replace(/\s/g, '').slice(0,20))) {
                                        result.phone_display = text;
                                        break;
                                    }
                                }
                            }
                        }

                        // Business profile pic: Look for img._ao3e with WhatsApp CDN URL
                        const profileImages = document.querySelectorAll('img._ao3e, img[src*="pps.whatsapp.net"], img[src*="cdn.whatsapp.net"]');
                        for (const img of profileImages) {
                            if (!img.src) continue;
                            // Must be a real profile pic URL (not placeholder)
                            if (img.src.includes('whatsapp.net') && !img.src.includes('default')) {
                                const rect = img.getBoundingClientRect();
                                // Profile image should be reasonably large
                                if (rect.width >= 80 && rect.height >= 80) {
                                    result.profile_pic = img.src;
                                    break;
                                }
                            }
                        }

                    } else {
                        // ===== PERSONAL WHATSAPP PROFILE =====
                        // Name: Look for "Name" label section with copyable-text span
                        const nameLabels = document.querySelectorAll('span');
                        let foundName = false;
                        for (const span of nameLabels) {
                            const text = span.textContent?.trim();
                            if (text === 'Name') {
                                foundName = true;
                                continue;
                            }
                            if (foundName && text && text.length > 0 && text.length < 50) {
                                const lower = text.toLowerCase();
                                // Skip common labels
                                if (!['name', 'edit', 'about', 'phone', 'profile', 'this is not your username'].includes(lower)) {
                                    result.name = text;
                                    break;
                                }
                            }
                        }

                        // Also try finding name in copyable-text span (more reliable)
                        if (!result.name) {
                            const copyableTexts = document.querySelectorAll('span.copyable-text, span._ao3e._aupe');
                            for (const span of copyableTexts) {
                                const text = span.textContent?.trim();
                                if (text && text.length > 0 && text.length < 50) {
                                    // Check if this span is in the Name section (not About)
                                    const parentText = span.closest('div')?.parentElement?.innerText || '';
                                    if (parentText.includes('Name') && !parentText.startsWith('About')) {
                                        result.name = text;
                                        break;
                                    }
                                }
                            }
                        }

                        // Personal phone: Look for phone icon then get phone number
                        const phoneIcon = document.querySelector('[data-icon="phone"]');
                        if (phoneIcon) {
                            let container = phoneIcon.closest('div[class*="x1c4vz4f"]') || phoneIcon.parentElement?.parentElement?.parentElement;
                            if (container) {
                                const spans = container.querySelectorAll('span');
                                for (const span of spans) {
                                    const text = span.textContent?.trim();
                                    if (text && /^\+?[\d\s\-()]+$/.test(text.replace(/\s/g, '').slice(0,20))) {
                                        result.phone_display = text;
                                        break;
                                    }
                                }
                            }
                        }

                        // Personal profile pic
                        const profileImages = document.querySelectorAll('img[src*="pps.whatsapp.net"], img[src*="cdn.whatsapp.net"]');
                        for (const img of profileImages) {
                            if (!img.src) continue;
                            if (!img.src.includes('default')) {
                                const rect = img.getBoundingClientRect();
                                if (rect.width >= 80 && rect.height >= 80 && rect.top < 400) {
                                    result.profile_pic = img.src;
                                    break;
                                }
                            }
                        }
                    }

                    // Get "About" or "Description" text based on account type
                    if (isBusiness) {
                        // For Business: Prioritize Description, fallback to About
                        // Get Description from input field
                        let description = null;
                        const descLabels = document.querySelectorAll('label');
                        for (const label of descLabels) {
                            if (label.textContent?.trim() === 'Description') {
                                const input = label.parentElement?.querySelector('input');
                                if (input && input.value && input.value.trim()) {
                                    description = input.value.trim();
                                }
                                break;
                            }
                        }

                        // Get About from Contact information section (look for info-refreshed icon section)
                        let aboutText = null;
                        const infoIcon = document.querySelector('[data-icon="info-refreshed"]');
                        if (infoIcon) {
                            const container = infoIcon.closest('div[class*="x1c4vz4f"]');
                            if (container) {
                                const spans = container.querySelectorAll('span');
                                let foundAboutLabel = false;
                                for (const span of spans) {
                                    const text = span.textContent?.trim();
                                    if (text === 'About') {
                                        foundAboutLabel = true;
                                        continue;
                                    }
                                    if (foundAboutLabel && text && text.length > 0 && text.length < 200) {
                                        aboutText = text;
                                        break;
                                    }
                                }
                            }
                        }

                        // Prioritize About, fallback to Description (matches Personal account behavior)
                        result.about = aboutText || description || null;
                    } else {
                        // For Personal: About is a simple label/value pair
                        const allSpans = document.querySelectorAll('span');
                        let foundAbout = false;
                        for (const span of allSpans) {
                            const text = span.textContent?.trim();
                            if (text === 'About') {
                                foundAbout = true;
                                continue;
                            }
                            if (foundAbout && text && text.length > 1 && text.length < 200) {
                                const lower = text.toLowerCase();
                                // Skip labels and common UI elements
                                if (!['about', 'edit', 'phone', 'profile', 'name'].includes(lower) &&
                                    !lower.includes('-refreshed') && !lower.includes('-icon')) {
                                    // Skip phone numbers
                                    const digitsOnly = text.replace(/[\s\-\(\)\+]/g, '');
                                    if (/^\d{6,}$/.test(digitsOnly)) {
                                        continue;
                                    }
                                    result.about = text;
                                    break;
                                }
                            }
                        }
                    }

                    return result;
                }''')

                # Log debug info from extraction
                debug_info = profile_data.get('debug', {})
                print(f"Bot {bot_profile_id}: Profile extraction debug: panelOpen={debug_info.get('profilePanelOpen')}, isBusiness={debug_info.get('isBusiness')}, hasProfileText={debug_info.get('hasProfileText')}, hasNameText={debug_info.get('hasNameText')}", flush=True)
                print(f"Bot {bot_profile_id}: Profile data: name={profile_data.get('name')}, profile_pic={bool(profile_data.get('profile_pic'))}", flush=True)
                logger.info(f"Bot {bot_profile_id}: Profile extraction debug: panelOpen={debug_info.get('profilePanelOpen')}, isBusiness={debug_info.get('isBusiness')}, hasProfileText={debug_info.get('hasProfileText')}, hasNameText={debug_info.get('hasNameText')}")

                if profile_data.get('name'):
                    account_info['name'] = profile_data['name']
                    account_info['push_name'] = profile_data['name']

                if profile_data.get('profile_pic'):
                    account_info['profile_pic'] = profile_data['profile_pic']

                if profile_data.get('about'):
                    account_info['about'] = profile_data['about']

                if profile_data.get('phone_display'):
                    account_info['phone_display'] = profile_data['phone_display']

                if profile_data.get('is_business'):
                    account_info['account_type'] = 'business'

                logger.info(f"Bot {bot_profile_id}: Profile data extracted: name={profile_data.get('name')}, phone_display={profile_data.get('phone_display')}, has_pic={bool(profile_data.get('profile_pic'))}, is_business={profile_data.get('is_business')}")

                # Close panels and return to main screen
                page.keyboard.press('Escape')
                page.wait_for_timeout(300)
                page.keyboard.press('Escape')
                page.wait_for_timeout(300)

            except Exception as e:
                print(f"Bot {bot_profile_id}: Profile extraction failed: {e}", flush=True)
                logger.warning(f"Bot {bot_profile_id}: Profile extraction failed: {e}")
                # Make sure to close any open panels
                try:
                    page.keyboard.press('Escape')
                    page.wait_for_timeout(200)
                    page.keyboard.press('Escape')
                except:
                    pass

            print(f"Bot {bot_profile_id}: Extracted account info: {account_info}", flush=True)
            logger.info(f"Bot {bot_profile_id}: Extracted account info: {account_info}")

        except Exception as e:
            print(f"Bot {bot_profile_id}: Could not extract account info: {e}", flush=True)
            logger.warning(f"Bot {bot_profile_id}: Could not extract account info: {e}")

        with get_db_session() as db:
            log = ActivityLog(
                bot_profile_id=bot_profile_id,
                action="bot_started",
                details="WhatsApp connected successfully"
            )
            db.add(log)
            profile = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
            if profile:
                # Check if this WhatsApp phone is already used by another bot
                if account_info.get('phone'):
                    # Check for running bots with same WhatsApp
                    existing_running_bot = db.query(BotProfile).filter(
                        BotProfile.whatsapp_phone == account_info['phone'],
                        BotProfile.id != bot_profile_id,
                        BotProfile.is_running == True
                    ).first()

                    if existing_running_bot:
                        logger.error(f"Bot {bot_profile_id}: WhatsApp phone {account_info['phone']} is already used by running bot {existing_running_bot.id} ({existing_running_bot.name})")
                        notify_status({
                            "status": "error",
                            "message": f"This WhatsApp account is already in use by another running bot: {existing_running_bot.name}. Please stop that bot first or use a different WhatsApp account."
                        })
                        # Stop this bot
                        instance.is_running = False
                        profile.is_running = False
                        profile.whatsapp_connected = False
                        db.commit()
                        return

                    # Check if this WhatsApp phone is used by a bot belonging to a DIFFERENT user
                    cross_user_bot = db.query(BotProfile).filter(
                        BotProfile.whatsapp_phone == account_info['phone'],
                        BotProfile.id != bot_profile_id,
                        BotProfile.user_id != profile.user_id
                    ).first()

                    if cross_user_bot:
                        logger.error(f"Bot {bot_profile_id}: WhatsApp phone {account_info['phone']} is already registered to a different user's bot {cross_user_bot.id}")
                        notify_status({
                            "status": "error",
                            "message": "This WhatsApp account is already registered to a different user. Each WhatsApp account can only be used by one user. Please use a different WhatsApp account."
                        })
                        # Stop this bot and clear session to force new QR on next start
                        instance.is_running = False
                        profile.is_running = False
                        profile.whatsapp_connected = False
                        db.commit()
                        # Clear this bot's session to force fresh QR code next time
                        session_path = str(SESSIONS_DIR / f"bot_{bot_profile_id}")
                        if os.path.exists(session_path):
                            try:
                                shutil.rmtree(session_path)
                                logger.info(f"Bot {bot_profile_id}: Cleared session after cross-user conflict")
                            except Exception as e:
                                logger.warning(f"Bot {bot_profile_id}: Failed to clear session: {e}")
                        return

                    # Clear WhatsApp info from other bots (same user only) that had this phone
                    other_bots = db.query(BotProfile).filter(
                        BotProfile.whatsapp_phone == account_info['phone'],
                        BotProfile.id != bot_profile_id,
                        BotProfile.user_id == profile.user_id  # Only clear same user's bots
                    ).all()

                    for other_bot in other_bots:
                        logger.info(f"Bot {bot_profile_id}: Clearing WhatsApp info from bot {other_bot.id} ({other_bot.name}) as this phone is now used by current bot")
                        other_bot.whatsapp_phone = None
                        other_bot.whatsapp_name = None
                        other_bot.whatsapp_push_name = None
                        other_bot.whatsapp_profile_pic = None
                        other_bot.whatsapp_about = None
                        other_bot.whatsapp_account_type = None
                        other_bot.whatsapp_connected = False

                profile.is_running = True
                profile.whatsapp_connected = True
                profile.last_active = datetime.utcnow()
                # Set timezone offset based on browser timezone
                # Priority: 1. Use detected_offset from proxy, 2. Calculate from timezone, 3. Fall back to message detection
                if detected_offset is not None:
                    # Use offset from proxy auto-detection
                    profile.whatsapp_timezone_offset = detected_offset
                    logger.info(f"Bot {bot_profile_id}: Using auto-detected offset from proxy: UTC{detected_offset:+d}")
                elif browser_timezone == 'UTC':
                    profile.whatsapp_timezone_offset = 0
                else:
                    # Calculate offset from timezone name using Python's zoneinfo
                    try:
                        from zoneinfo import ZoneInfo
                        tz = ZoneInfo(browser_timezone)
                        # Get current offset in hours
                        now = datetime.now(tz)
                        offset_seconds = now.utcoffset().total_seconds()
                        profile.whatsapp_timezone_offset = int(offset_seconds / 3600)
                        logger.info(f"Bot {bot_profile_id}: Calculated timezone offset for {browser_timezone}: UTC{profile.whatsapp_timezone_offset:+d}")
                    except Exception as tz_err:
                        logger.warning(f"Bot {bot_profile_id}: Could not calculate timezone offset for {browser_timezone}: {tz_err}")
                        # Leave as None - detection will happen on first message
                        profile.whatsapp_timezone_offset = None

                # Also save the detected/configured timezone to the profile for future reference
                profile.browser_timezone = browser_timezone
                # Update account info if extracted
                if account_info.get('phone'):
                    profile.whatsapp_phone = account_info['phone']
                if account_info.get('name'):
                    profile.whatsapp_name = account_info['name']
                if account_info.get('push_name'):
                    profile.whatsapp_push_name = account_info['push_name']
                if account_info.get('profile_pic'):
                    profile.whatsapp_profile_pic = account_info['profile_pic']
                    logger.info(f"Bot {bot_profile_id}: Saved profile pic to database")
                else:
                    logger.warning(f"Bot {bot_profile_id}: No profile pic extracted from WhatsApp")
                if account_info.get('about'):
                    profile.whatsapp_about = account_info['about']
                if account_info.get('account_type'):
                    profile.whatsapp_account_type = account_info['account_type']

                # Explicit commit to ensure profile data is saved before status notification
                db.commit()
                logger.info(f"Bot {bot_profile_id}: Profile data committed to database")

        logger.info(f"Bot {bot_profile_id}: WhatsApp connected, starting message listener")
        # Send status with account info so frontend can update the bot card
        status_data = {
            "status": "running",
            "message": "Bot is running and listening for messages...",
            "account_info": {
                "phone": account_info.get('phone'),
                "phone_display": account_info.get('phone_display'),  # Formatted phone from profile
                "name": account_info.get('name'),
                "push_name": account_info.get('push_name'),
                "profile_pic": account_info.get('profile_pic'),
                "about": account_info.get('about'),
                "account_type": account_info.get('account_type')
            },
            "ai_response_enabled": instance.ai_response_enabled,
            "history_sync_active": instance.history_sync_active,
            "history_sync_progress": instance.history_sync_progress,
        }
        notify_status(status_data)

        # Quick sync: Just sync conversation list and profile pics from sidebar
        # Message sync happens periodically in the background
        try:
            logger.info(f"Bot {bot_profile_id}: Quick sync - extracting conversations from WhatsApp sidebar...")
            _quick_sync_conversations(page, bot_profile_id, notify_status)
        except Exception as e:
            logger.warning(f"Bot {bot_profile_id}: Failed to quick sync: {e}")
            # Fall back to just profile pics
            try:
                _update_all_profile_pics(page, bot_profile_id)
            except Exception as e2:
                logger.warning(f"Bot {bot_profile_id}: Failed to update profile pictures: {e2}")

        processed_messages = set()
        saved_media_hashes = {}  # Track saved media by hash per chat: {chat_name: {content_hash: file_info}}
        processed_chats_cooldown = {}  # Track when each chat was last processed
        last_processed_message_hash = {}  # Track last message hash per chat to detect truly new messages
        CHAT_COOLDOWN_SECONDS = 5  # Wait 5 seconds before re-processing same chat (short for quick back-and-forth)
        MAX_PROCESSED_MESSAGES = 1000  # Limit set size to prevent memory growth
        loop_count = 0

        # Periodic sync settings
        last_sync_time = time.time()
        SYNC_INTERVAL_SECONDS = 180  # Sync messages every 3 minutes
        SYNC_BATCH_SIZE = 5  # Sync 5 conversations per batch

        logger.info(f"Bot {bot_profile_id}: Entering main message loop")

        # IMPORTANT: Close any open chat windows before starting the message loop
        # If a chat is open, we might miss unread indicators on other chats
        try:
            logger.info(f"Bot {bot_profile_id}: Closing any open chat windows before starting...")
            for _ in range(3):  # Press Escape multiple times to ensure chat is closed
                page.keyboard.press('Escape')
                time.sleep(0.3)
            logger.info(f"Bot {bot_profile_id}: Chat windows closed, ready to detect unread messages")
        except Exception as close_err:
            logger.debug(f"Bot {bot_profile_id}: Could not close chat windows: {close_err}")

        while instance.is_running:
            try:
                # === Check browser connectivity ===
                if not _is_browser_connected(page, context):
                    instance.browser_connected = False
                    instance.browser_recovery_attempts += 1
                    logger.warning(f"Bot {bot_profile_id}: Browser disconnected! Recovery attempt {instance.browser_recovery_attempts}/{instance.max_browser_recovery_attempts}")
                    print(f"Bot {bot_profile_id}: Browser disconnected! Recovery attempt {instance.browser_recovery_attempts}/{instance.max_browser_recovery_attempts}", flush=True)

                    if instance.browser_recovery_attempts > instance.max_browser_recovery_attempts:
                        logger.error(f"Bot {bot_profile_id}: Max browser recovery attempts exceeded. Stopping bot.")
                        notify_status({
                            "status": "error",
                            "message": "Browser closed unexpectedly. Max recovery attempts exceeded. Please restart the bot.",
                            "browser_connected": False
                        })
                        instance.is_running = False
                        break

                    # Notify user about disconnection
                    notify_status({
                        "status": "reconnecting",
                        "message": f"Browser disconnected. Attempting recovery ({instance.browser_recovery_attempts}/{instance.max_browser_recovery_attempts})...",
                        "browser_connected": False
                    })

                    # Attempt to recover - close old context and relaunch
                    try:
                        if context:
                            try:
                                context.close()
                            except Exception:
                                pass

                        # Relaunch browser with same settings
                        headless = config.get('headless', False)
                        context_options = {
                            "headless": headless,
                            "viewport": {"width": 1280, "height": 900},
                            "args": [
                                "--disable-blink-features=AutomationControlled",
                                "--no-sandbox"
                            ]
                        }

                        # Add proxy if configured
                        if config.get('proxy_enabled') and config.get('proxy_url'):
                            proxy_config = {"server": config['proxy_url']}
                            if config.get('proxy_username'):
                                proxy_config["username"] = config['proxy_username']
                            if config.get('proxy_password'):
                                proxy_config["password"] = config['proxy_password']
                            context_options["proxy"] = proxy_config

                        context = playwright.chromium.launch_persistent_context(
                            user_data_dir=session_path,
                            **context_options
                        )

                        context.add_init_script('''
                            Object.defineProperty(navigator, 'webdriver', {
                                get: () => undefined
                            });
                        ''')

                        if context.pages:
                            page = context.pages[0]
                        else:
                            page = context.new_page()

                        # Store page reference
                        with _bot_pages_lock:
                            _bot_pages[bot_profile_id] = page

                        # Minimize the browser window
                        try:
                            cdp = context.new_cdp_session(page)
                            window_info = cdp.send("Browser.getWindowForTarget")
                            window_id = window_info.get("windowId")
                            if window_id:
                                cdp.send("Browser.setWindowBounds", {
                                    "windowId": window_id,
                                    "bounds": {"windowState": "minimized"}
                                })
                        except Exception:
                            pass

                        # Reload WhatsApp Web
                        page.goto("https://web.whatsapp.com", wait_until="networkidle")

                        # Wait for reconnection (session should auto-login)
                        reconnected = False
                        for i in range(30):  # Wait up to 30 seconds
                            if not instance.is_running:
                                break
                            auth_selectors = [
                                '[data-testid="chat-list"]',
                                '[data-testid="chatlist-header"]',
                                '[aria-label="Chat list"]',
                                '[data-testid="side"]'
                            ]
                            for selector in auth_selectors:
                                if page.query_selector(selector):
                                    reconnected = True
                                    break
                            if reconnected:
                                break
                            time.sleep(1)

                        if reconnected:
                            instance.browser_connected = True
                            instance.browser_started_at = datetime.now()  # Reset timer after recovery
                            instance.browser_recovery_attempts = 0
                            instance.whatsapp_connected = True
                            logger.info(f"Bot {bot_profile_id}: Browser recovered successfully!")
                            print(f"Bot {bot_profile_id}: Browser recovered successfully!", flush=True)
                            notify_status({
                                "status": "running",
                                "message": "Browser recovered. Bot is running.",
                                "browser_connected": True,
                                "whatsapp_connected": True
                            })
                            time.sleep(2)
                            continue
                        else:
                            # Might need QR scan again
                            logger.warning(f"Bot {bot_profile_id}: Browser recovered but WhatsApp needs re-authentication")
                            notify_status({
                                "status": "waiting_qr",
                                "message": "Browser recovered but WhatsApp session expired. Please scan QR code.",
                                "browser_connected": True,
                                "whatsapp_connected": False
                            })
                            # Let the loop continue - user needs to scan QR
                            time.sleep(5)
                            continue

                    except Exception as recovery_error:
                        logger.error(f"Bot {bot_profile_id}: Browser recovery failed: {recovery_error}")
                        print(f"Bot {bot_profile_id}: Browser recovery failed: {recovery_error}", flush=True)
                        time.sleep(5)
                        continue
                else:
                    # Browser is connected - reset recovery counter
                    if instance.browser_recovery_attempts > 0:
                        instance.browser_recovery_attempts = 0
                    instance.browser_connected = True

                loop_count += 1
                current_time = time.time()

                # === History sync takes EXCLUSIVE control of the page ===
                if instance.history_sync_requested and not instance.history_sync_active:
                    _run_full_history_sync(page, instance, bot_profile_id, notify_status)
                    time.sleep(2)
                    continue

                if instance.history_sync_active:
                    time.sleep(1)
                    continue

                # Cleanup processed_messages set if it gets too large
                if len(processed_messages) > MAX_PROCESSED_MESSAGES:
                    # Keep only the most recent half
                    processed_messages.clear()
                    logger.info(f"Bot {bot_profile_id}: Cleared processed_messages set (was > {MAX_PROCESSED_MESSAGES})")

                # Cleanup processed_chats_cooldown dict to prevent memory leak
                # This dict tracks when each chat was last processed for rate limiting
                MAX_COOLDOWN_ENTRIES = 500
                if len(processed_chats_cooldown) > MAX_COOLDOWN_ENTRIES:
                    processed_chats_cooldown.clear()
                    logger.info(f"Bot {bot_profile_id}: Cleared processed_chats_cooldown dict (was > {MAX_COOLDOWN_ENTRIES})")

                # Cleanup last_processed_message_hash dict to prevent memory leak
                # This dict tracks last message hash per chat for deduplication
                MAX_HASH_ENTRIES = 500
                if len(last_processed_message_hash) > MAX_HASH_ENTRIES:
                    last_processed_message_hash.clear()
                    logger.info(f"Bot {bot_profile_id}: Cleared last_processed_message_hash dict (was > {MAX_HASH_ENTRIES})")

                # Auto browser restart for memory management
                # Check if browser has been running longer than auto_restart_hours
                if instance.browser_started_at and instance.auto_restart_hours > 0:
                    hours_running = (datetime.now() - instance.browser_started_at).total_seconds() / 3600
                    if hours_running >= instance.auto_restart_hours:
                        logger.info(f"Bot {bot_profile_id}: Auto-restarting browser after {hours_running:.1f} hours for memory management")
                        print(f"Bot {bot_profile_id}: Auto-restarting browser after {hours_running:.1f} hours...", flush=True)
                        notify_status({
                            "status": "restarting",
                            "message": f"Auto-restarting browser for memory management ({hours_running:.1f}h uptime)..."
                        })

                        try:
                            # Close current browser
                            instance.browser_connected = False
                            context.close()
                            playwright.stop()
                            logger.info(f"Bot {bot_profile_id}: Browser closed for auto-restart")

                            # Small delay before relaunch
                            time.sleep(3)

                            # Relaunch browser with same settings
                            playwright = sync_playwright().start()
                            session_dir = str(SESSIONS_DIR / f"bot_{bot_profile_id}")
                            os.makedirs(session_dir, exist_ok=True)

                            browser_args = [
                                '--disable-blink-features=AutomationControlled',
                                '--disable-infobars',
                                '--no-sandbox',
                                '--disable-dev-shm-usage',
                                '--disable-gpu',
                            ]

                            context = playwright.chromium.launch_persistent_context(
                                user_data_dir=session_dir,
                                headless=False,
                                args=browser_args,
                                viewport={"width": 1280, "height": 800},
                                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                            )

                            if context.pages:
                                page = context.pages[0]
                            else:
                                page = context.new_page()

                            # Store page reference
                            with _bot_pages_lock:
                                _bot_pages[bot_profile_id] = page

                            # Minimize browser window
                            try:
                                cdp = context.new_cdp_session(page)
                                window_info = cdp.send("Browser.getWindowForTarget")
                                window_id = window_info.get("windowId")
                                if window_id:
                                    cdp.send("Browser.setWindowBounds", {
                                        "windowId": window_id,
                                        "bounds": {"windowState": "minimized"}
                                    })
                            except Exception:
                                pass

                            # Navigate to WhatsApp Web
                            page.goto("https://web.whatsapp.com", wait_until="networkidle")

                            # Wait for WhatsApp to reconnect (session should auto-login)
                            reconnected = False
                            for i in range(30):
                                if not instance.is_running:
                                    break
                                auth_selectors = [
                                    '[data-testid="chat-list"]',
                                    '[data-testid="chatlist-header"]',
                                    '[aria-label="Chat list"]',
                                    '[data-testid="side"]'
                                ]
                                for selector in auth_selectors:
                                    if page.query_selector(selector):
                                        reconnected = True
                                        break
                                if reconnected:
                                    break
                                time.sleep(1)

                            if reconnected:
                                instance.browser_connected = True
                                instance.browser_started_at = datetime.now()
                                instance.whatsapp_connected = True
                                logger.info(f"Bot {bot_profile_id}: Browser auto-restart successful!")
                                print(f"Bot {bot_profile_id}: Browser auto-restart successful!", flush=True)
                                notify_status({
                                    "status": "running",
                                    "message": "Browser restarted successfully. Bot is running.",
                                    "browser_connected": True,
                                    "whatsapp_connected": True
                                })
                            else:
                                logger.warning(f"Bot {bot_profile_id}: Browser restarted but WhatsApp needs re-authentication")
                                notify_status({
                                    "status": "waiting_qr",
                                    "message": "Browser restarted but WhatsApp session expired. Please scan QR code.",
                                    "browser_connected": True,
                                    "whatsapp_connected": False
                                })

                            # Clear memory tracking dicts after restart
                            processed_messages.clear()
                            processed_chats_cooldown.clear()
                            last_processed_message_hash.clear()
                            logger.info(f"Bot {bot_profile_id}: Cleared all memory tracking dicts after browser restart")

                            time.sleep(2)
                            continue

                        except Exception as restart_error:
                            logger.error(f"Bot {bot_profile_id}: Browser auto-restart failed: {restart_error}")
                            print(f"Bot {bot_profile_id}: Browser auto-restart failed: {restart_error}", flush=True)
                            notify_status({
                                "status": "error",
                                "message": f"Browser auto-restart failed: {restart_error}"
                            })
                            time.sleep(5)
                            continue

                # Process any pending outgoing messages (manual sends from dashboard)
                # These ALWAYS run regardless of AI toggle state
                try:
                    _process_outgoing_messages(page, bot_profile_id)
                except Exception as e:
                    logger.error(f"Bot {bot_profile_id}: Error processing outgoing messages: {e}")

                # Process any pending outgoing files (manual file sends from dashboard)
                try:
                    _process_outgoing_files(page, bot_profile_id)
                except Exception as e:
                    logger.error(f"Bot {bot_profile_id}: Error processing outgoing files: {e}")

                # Process any pending create-group requests
                try:
                    _process_create_group(page, bot_profile_id)
                except Exception as e:
                    logger.error(f"Bot {bot_profile_id}: Error processing create group: {e}")

                # === Only check unreads + AI respond if toggle is ON ===
                if not instance.ai_response_enabled:
                    time.sleep(2)
                    continue

                if loop_count % 15 == 1:  # Log every 30 seconds (15 * 2 sec sleep)
                    # Debug: check page state
                    try:
                        page_title = page.title()
                        chat_list = page.query_selector('[aria-label="Chat list"]')
                        print(f"Bot {bot_profile_id}: Message listener active, loop: {loop_count}, Page: {page_title[:30] if page_title else 'N/A'}, ChatList: {chat_list is not None}", flush=True)
                    except Exception as pe:
                        print(f"Bot {bot_profile_id}: Message listener active, loop: {loop_count}, Error: {pe}", flush=True)
                    logger.info(f"Bot {bot_profile_id}: Message listener active, checking for unread messages...")
                    # Debug: check page state
                    try:
                        page_title = page.title()
                        chat_list = page.query_selector('[aria-label="Chat list"]')
                        logger.info(f"Bot {bot_profile_id}: Page title: {page_title}, Chat list visible: {chat_list is not None}")
                    except Exception as e:
                        logger.warning(f"Bot {bot_profile_id}: Debug check failed: {e}")

                # Close any open chat windows before checking for unread messages
                # This ensures we can see unread indicators on the chat list
                try:
                    page.keyboard.press('Escape')
                    time.sleep(0.2)
                except Exception:
                    pass  # Ignore errors - just a precaution


                # NOTE: Periodic sync disabled - using on-demand sync instead
                # History is now synced when responding to a conversation for the first time
                # This makes the bot faster and uses less resources
                # if current_time - last_sync_time >= SYNC_INTERVAL_SECONDS:
                #     try:
                #         logger.info(f"Bot {bot_profile_id}: Running periodic message sync...")
                #         synced_count = _sync_messages_batch(
                #             page, bot_profile_id, None, SYNC_BATCH_SIZE
                #         )
                #         last_sync_time = current_time
                #         if synced_count > 0:
                #             logger.info(f"Bot {bot_profile_id}: Periodic sync completed - {synced_count} conversations synced")
                #     except Exception as sync_err:
                #         logger.warning(f"Bot {bot_profile_id}: Periodic sync error: {sync_err}")

                # Try multiple selectors for unread messages
                unread_selectors = [
                    '[data-testid="icon-unread-count"]',
                    '[aria-label*="unread message"]',
                    'span[data-testid="icon-unread-count"]',
                    '[data-icon="unread-count"]',
                    # Additional selectors for WhatsApp Business
                    'span[aria-label*="unread"]',
                    'div[aria-label*="unread"]',
                    '[class*="unread"]',
                    # More selectors for WhatsApp Web 2025/2026
                    '[data-testid="unread-count"]',
                    'span[data-icon="unread"]',
                    '[aria-label*="new message"]',
                    'span[data-testid*="unread"]',
                ]

                unread_indicators = []
                for selector in unread_selectors:
                    try:
                        # IMPORTANT: Only match elements inside #pane-side (sidebar)
                        # Without this filter, we match floating elements that don't clear when chat opens
                        sidebar_selector = f'#pane-side {selector}'
                        found = page.query_selector_all(sidebar_selector)
                        if found:
                            unread_indicators = found
                            break
                    except:
                        pass

                if unread_indicators and loop_count % 5 == 0:
                    print(f"Bot {bot_profile_id}: Found {len(unread_indicators)} unread chat(s)", flush=True)
                    logger.info(f"Bot {bot_profile_id}: Found {len(unread_indicators)} unread chat(s)")

                # Alternative: Check current open chat for new messages
                if not unread_indicators:
                    try:
                        # Check if a chat is currently open - try multiple selectors for WhatsApp Business
                        header_check_selectors = [
                            '#main header',  # WhatsApp Business
                            '[data-testid="conversation-header"]',
                            '[data-testid="conversation-info-header"]',
                        ]
                        header = None
                        for h_sel in header_check_selectors:
                            header = page.query_selector(h_sel)
                            if header:
                                break

                        if loop_count % 15 == 1:
                            print(f"Bot {bot_profile_id}: No unread indicators, checking open chat. Header found: {header is not None}", flush=True)

                        # If no chat is open, periodically click the first chat row to check for messages
                        # Run on first loop and every 60 loops (~2 minutes) when no unread detected
                        if not header and (loop_count == 1 or loop_count % 60 == 0):
                            try:
                                chat_rows = page.query_selector_all('[role="row"]')
                                if chat_rows and len(chat_rows) > 0:
                                    chat_rows[0].click()
                                    time.sleep(2)

                                    # Try multiple header selectors for WhatsApp Business
                                    header_selectors = [
                                        '[data-testid="conversation-header"]',
                                        '[data-testid="conversation-info-header"]',
                                        '#main header',
                                        '[data-testid="conversation-panel-header"]',
                                        '#main [data-testid*="header"]',
                                        '#main [role="banner"]',
                                    ]
                                    for h_sel in header_selectors:
                                        header = page.query_selector(h_sel)
                                        if header:
                                            break
                            except Exception as ce:
                                logger.debug(f"Bot {bot_profile_id}: Click error: {ce}")

                        if header:
                            # Get last incoming message - try multiple selectors for WhatsApp Business
                            incoming_msgs = page.query_selector_all('[data-testid="msg-container"]:not([data-testid="msg-container"][class*="message-out"])')
                            if not incoming_msgs:
                                # WhatsApp Business uses different classes
                                incoming_msgs = page.query_selector_all('.message-in')
                            if not incoming_msgs:
                                # Fallback: elements with data-id that are incoming
                                # Include @lid format for WhatsApp 2025+ private chats
                                incoming_msgs = page.query_selector_all('#main [data-id*="@c.us"]:not([data-id*="true_"]), #main [data-id*="@g.us"]:not([data-id*="true_"]), #main [data-id*="@lid"]:not([data-id*="true_"])')
                            if incoming_msgs:
                                last_msg = incoming_msgs[-1]

                                # Check for media content in the message
                                media_info_open = page.evaluate(r'''(msgEl) => {
                                    let mediaUrl = null;
                                    let mediaType = null;
                                    let mediaFilename = null;

                                    // IMPORTANT: Check IMAGES FIRST because document selectors can be too broad

                                    // IMAGE detection FIRST - these are very specific selectors
                                    const imageThumb = msgEl.querySelector('[data-testid="image-thumb"]') ||
                                                      msgEl.querySelector('[data-testid="media-state-photo"]');
                                    const imageImg = msgEl.querySelector('[data-testid="image-thumb"] img') ||
                                                    msgEl.querySelector('img[data-testid="media-canvas"]') ||
                                                    msgEl.querySelector('div[role="button"] img[src*="blob:"]');

                                    let isDefinitelyImage = false;
                                    if (imageThumb || (imageImg && imageImg.src && !imageImg.src.includes('pps.whatsapp.net'))) {
                                        isDefinitelyImage = true;
                                        if (imageImg && imageImg.src && imageImg.src.startsWith('blob:')) {
                                            mediaUrl = imageImg.src;
                                            mediaType = 'image';
                                        } else {
                                            mediaUrl = 'image-click-to-download:image/jpeg';
                                            mediaType = 'image';
                                        }
                                    }

                                    // Document detection - ONLY if not an image (strict selectors)
                                    const docSelectors = [
                                        '[data-testid="document-thumb"]',
                                        '[data-testid="document"]',
                                        '[data-testid="pdf-thumb"]',
                                        '[data-testid="media-state-document"]',
                                        '[data-testid="msg-document"]',
                                        'span[data-icon="pdf"]',
                                        'span[data-icon="doc"]',
                                        'span[data-icon="document"]'
                                    ];

                                    let docContainer = null;
                                    let downloadLink = null;
                                    let hasFileExtension = false;
                                    let detectedFilenameFromText = null;

                                    if (!isDefinitelyImage) {
                                        for (const selector of docSelectors) {
                                            try {
                                                docContainer = msgEl.querySelector(selector);
                                                if (docContainer) break;
                                            } catch (e) {}
                                        }

                                        downloadLink = msgEl.querySelector('a[download]') || msgEl.querySelector('a[href*="blob:"]');

                                        // ENHANCED: Check for file extension in text/title (for group chats)
                                        if (!docContainer && !downloadLink) {
                                            const allText = msgEl.innerText || msgEl.textContent || '';
                                            const fileExtRegex = /([^\s<>"']+\.(pdf|docx?|xlsx?|pptx?|csv|txt|zip|rar))/i;
                                            const match = allText.match(fileExtRegex);
                                            if (match) {
                                                hasFileExtension = true;
                                                detectedFilenameFromText = match[1].trim();
                                            }
                                            // Also check title attributes
                                            const elementsWithTitle = msgEl.querySelectorAll('[title]');
                                            for (const el of elementsWithTitle) {
                                                const title = el.getAttribute('title') || '';
                                                const titleMatch = title.match(fileExtRegex);
                                                if (titleMatch) {
                                                    hasFileExtension = true;
                                                    detectedFilenameFromText = titleMatch[1].trim();
                                                    docContainer = el;
                                                    break;
                                                }
                                            }
                                        }
                                    }

                                    if (!isDefinitelyImage && (docContainer || downloadLink || hasFileExtension)) {
                                        const docLink = msgEl.querySelector('a[href*="blob:"]') || msgEl.querySelector('a[download]');
                                        if (docLink) {
                                            mediaUrl = docLink.href;
                                        }
                                        const filenameEl = msgEl.querySelector('[data-testid="document-name"]') ||
                                                          msgEl.querySelector('span[title*="."]') ||
                                                          msgEl.querySelector('[role="button"][title]');
                                        if (filenameEl) {
                                            mediaFilename = filenameEl.getAttribute('title') || filenameEl.innerText;
                                            // Clean filename: extract from 'Download "filename.ext"' format
                                            if (mediaFilename) {
                                                const downloadMatch = mediaFilename.match(/Download\s+"([^"]+)"/);
                                                if (downloadMatch) {
                                                    mediaFilename = downloadMatch[1];
                                                } else {
                                                    // Remove leading/trailing quotes and whitespace
                                                    mediaFilename = mediaFilename.replace(/^["'\s]+|["'\s]+$/g, '');
                                                }
                                            }
                                        }
                                        // Use detected filename if no other found
                                        if (!mediaFilename && detectedFilenameFromText) {
                                            mediaFilename = detectedFilenameFromText;
                                        }
                                        mediaType = 'document';
                                        // If no blob URL, mark for click-to-download
                                        if (!mediaUrl || !mediaUrl.startsWith('blob:')) {
                                            let docMime = 'application/octet-stream';
                                            if (mediaFilename) {
                                                // Extract extension and clean non-alphanumeric chars
                                                const docExt = mediaFilename.split('.').pop().toLowerCase().replace(/[^a-z0-9]/g, '');
                                                const docMimeTypes = {
                                                    'pdf': 'application/pdf',
                                                    'doc': 'application/msword',
                                                    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                                                    'xls': 'application/vnd.ms-excel',
                                                    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                                    'ppt': 'application/vnd.ms-powerpoint',
                                                    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                                                    'csv': 'text/csv',
                                                    'txt': 'text/plain',
                                                    'zip': 'application/zip',
                                                    'rar': 'application/x-rar-compressed'
                                                };
                                                docMime = docMimeTypes[docExt] || docMime;
                                            }
                                            mediaUrl = 'document-click-to-download:' + docMime;
                                        }
                                    }

                                    // Image detection - check AFTER documents
                                    if (!mediaUrl) {
                                        const imageContainer = msgEl.querySelector('[data-testid="image-thumb"]') ||
                                                              msgEl.querySelector('[data-testid="media-state-photo"]') ||
                                                              msgEl.querySelector('div[role="button"][class*="image"]');
                                        const imageThumb = msgEl.querySelector('[data-testid="image-thumb"] img') ||
                                                          msgEl.querySelector('img[data-testid="media-canvas"]') ||
                                                          msgEl.querySelector('[data-testid="media-url-provider"] img') ||
                                                          msgEl.querySelector('div[role="button"] img[src*="blob:"]');
                                        if (imageThumb && imageThumb.src && !imageThumb.src.includes('pps.whatsapp.net')) {
                                            if (imageThumb.src.startsWith('blob:')) {
                                                mediaUrl = imageThumb.src;
                                                mediaType = 'image';
                                            } else {
                                                mediaUrl = 'image-click-to-download:image/jpeg';
                                                mediaType = 'image';
                                            }
                                        } else if (imageContainer && !imageThumb) {
                                            mediaUrl = 'image-click-to-download:image/jpeg';
                                            mediaType = 'image';
                                        }
                                    }

                                    // Video detection
                                    if (!mediaUrl) {
                                        const videoThumb = msgEl.querySelector('[data-testid="video-thumb"] img') ||
                                                          msgEl.querySelector('video source') ||
                                                          msgEl.querySelector('video');
                                        if (videoThumb) {
                                            mediaUrl = videoThumb.src || videoThumb.querySelector('source')?.src;
                                            mediaType = 'video';
                                        }
                                    }

                                    // Sticker/Emoji detection
                                    if (!mediaUrl) {
                                        const sticker = msgEl.querySelector('[data-testid="sticker"] img') ||
                                                       msgEl.querySelector('img[data-testid="sticker"]') ||
                                                       msgEl.querySelector('[data-testid="x-sticker"] img') ||
                                                       msgEl.querySelector('[data-testid="animated-emoji"] img') ||
                                                       msgEl.querySelector('img[data-testid="animated-emoji"]');
                                        if (sticker && sticker.src) {
                                            mediaUrl = sticker.src;
                                            mediaType = 'sticker';
                                        }
                                    }

                                    return { mediaUrl, mediaType, mediaFilename };
                                }''', last_msg)

                                text_elem = last_msg.query_selector('[data-testid="msg-text"]')
                                text = text_elem.inner_text() if text_elem else ''

                                # If no text but has media, use placeholder
                                if not text and media_info_open and media_info_open.get('mediaUrl'):
                                    text = f"[{media_info_open.get('mediaType', 'media')}]"

                                if text:
                                    # Extract chat name using JavaScript for more precise control
                                    chat_name = page.evaluate('''(header) => {
                                        // Method 1: Standard WhatsApp Web selector
                                        let nameElem = header.querySelector('[data-testid="conversation-info-header-chat-title"]');
                                        if (nameElem) {
                                            const text = nameElem.textContent?.trim();
                                            if (text) return text;
                                        }

                                        // Method 2: WhatsApp Business - look for contact/group name section
                                        // The name is typically in the clickable section that opens contact info
                                        // It's the first span with title that is NOT status text
                                        const invalidPatterns = [
                                            /^last seen/i,
                                            /^online$/i,
                                            /^typing/i,
                                            /^click here/i,
                                            /^\\d+ participants?$/i,
                                            /^tap here/i,
                                            /AM$|PM$/i  // Time patterns
                                        ];

                                        // Find all spans with title attribute
                                        const spans = header.querySelectorAll('span[title]');
                                        for (const span of spans) {
                                            const title = span.getAttribute('title');
                                            if (title && title.trim()) {
                                                // Check if this looks like a status text
                                                const isInvalid = invalidPatterns.some(pattern => pattern.test(title.trim()));
                                                if (!isInvalid && title.trim().length > 0) {
                                                    return title.trim();
                                                }
                                            }
                                        }

                                        // Method 3: Look for the first text node in header that looks like a name
                                        const textSpans = header.querySelectorAll('span');
                                        for (const span of textSpans) {
                                            const text = span.textContent?.trim();
                                            if (text && text.length > 0 && text.length < 50) {
                                                const isInvalid = invalidPatterns.some(pattern => pattern.test(text));
                                                if (!isInvalid && !text.includes('click') && !text.includes('tap')) {
                                                    // Check if this span is likely the name (has direct text, no child elements with text)
                                                    const childText = Array.from(span.children).map(c => c.textContent).join('');
                                                    if (span.textContent.trim() === text && childText.length === 0) {
                                                        return text;
                                                    }
                                                }
                                            }
                                        }

                                        return "Unknown";
                                    }''', header)

                                    if not chat_name:
                                        chat_name = "Unknown"

                                    # Extract FULL data-id from the message element or its parent/child (most reliable),
                                    # then fall back to header or sidebar
                                    # The data-id might be on the element itself, a parent, or a child element
                                    msg_data_id = page.evaluate('''(msgEl) => {
                                        // Check element itself
                                        if (msgEl.getAttribute('data-id')) {
                                            return msgEl.getAttribute('data-id');
                                        }
                                        // Check parent elements up to 3 levels
                                        let parent = msgEl.parentElement;
                                        for (let i = 0; i < 3 && parent; i++) {
                                            if (parent.getAttribute('data-id')) {
                                                return parent.getAttribute('data-id');
                                            }
                                            parent = parent.parentElement;
                                        }
                                        // Check closest msg-container
                                        const container = msgEl.closest('[data-testid="msg-container"]');
                                        if (container && container.getAttribute('data-id')) {
                                            return container.getAttribute('data-id');
                                        }
                                        // Check closest element with data-id
                                        const closest = msgEl.closest('[data-id]');
                                        if (closest) {
                                            return closest.getAttribute('data-id');
                                        }
                                        // Check child elements
                                        const childWithDataId = msgEl.querySelector('[data-id]');
                                        if (childWithDataId) {
                                            return childWithDataId.getAttribute('data-id');
                                        }
                                        return null;
                                    }''', last_msg)

                                    chat_info_open = page.evaluate('''(args) => {
                                        const { msgDataId, chatName } = args;
                                        let fullDataId = null;
                                        let isGroup = false;
                                        let extractionMethod = 'none';

                                        // Method 0 (BEST): Extract from the message's own data-id
                                        // Format: "false_PHONE@c.us_MSGID" or "false_GROUPID@g.us_MSGID"
                                        if (msgDataId) {
                                            // Extract the chat part (between first _ and second _)
                                            const parts = msgDataId.split('_');
                                            if (parts.length >= 2) {
                                                // Find the part that contains @c.us or @g.us
                                                for (let i = 1; i < parts.length; i++) {
                                                    const part = parts[i];
                                                    if (part.includes('@c.us')) {
                                                        fullDataId = part;
                                                        isGroup = false;
                                                        extractionMethod = 'message_data_id';
                                                        break;
                                                    } else if (part.includes('@g.us')) {
                                                        fullDataId = part;
                                                        isGroup = true;
                                                        extractionMethod = 'message_data_id';
                                                        break;
                                                    }
                                                }
                                            }
                                        }

                                        // Method 1: Check conversation header for data-id attributes
                                        if (!fullDataId) {
                                            const header = document.querySelector('[data-testid="conversation-header"]');
                                            if (header) {
                                                const headerWithDataId = header.querySelector('[data-id]');
                                                if (headerWithDataId) {
                                                    const dataId = headerWithDataId.getAttribute('data-id');
                                                    if (dataId && (dataId.includes('@c.us') || dataId.includes('@g.us'))) {
                                                        fullDataId = dataId;
                                                        isGroup = dataId.includes('@g.us');
                                                        extractionMethod = 'header';
                                                    }
                                                }
                                            }
                                        }

                                        // Method 2: Search messages in main panel for data-ids
                                        // This is more reliable than sidebar for the CURRENTLY OPEN chat
                                        if (!fullDataId) {
                                            const mainPanel = document.querySelector('#main') ||
                                                             document.querySelector('[data-testid="conversation-panel-messages"]');
                                            if (mainPanel) {
                                                const msgElements = mainPanel.querySelectorAll('[data-id]');
                                                for (const el of msgElements) {
                                                    const id = el.getAttribute('data-id');
                                                    if (!id) continue;

                                                    // Extract chat ID from message data-id
                                                    const parts = id.split('_');
                                                    for (let i = 1; i < parts.length; i++) {
                                                        const part = parts[i];
                                                        if (part.includes('@g.us')) {
                                                            fullDataId = part;
                                                            isGroup = true;
                                                            extractionMethod = 'main_panel';
                                                            break;
                                                        } else if (part.includes('@c.us')) {
                                                            fullDataId = part;
                                                            isGroup = false;
                                                            extractionMethod = 'main_panel';
                                                            break;
                                                        }
                                                    }
                                                    if (fullDataId) break;
                                                }
                                            }
                                        }

                                        // Method 3: Check for group icon in header (for isGroup only)
                                        if (!isGroup) {
                                            const header = document.querySelector('[data-testid="conversation-header"]');
                                            if (header) {
                                                const hasGroupIcon = header.querySelector('[data-icon="default-group"]') !== null ||
                                                                    header.querySelector('[data-testid="default-group"]') !== null;
                                                if (hasGroupIcon) isGroup = true;
                                            }
                                        }

                                        return { fullDataId: fullDataId, isGroup: isGroup, method: extractionMethod };
                                    }''', {'msgDataId': msg_data_id, 'chatName': chat_name})

                                    chat_data_id = chat_info_open.get('fullDataId') if chat_info_open else None

                                    # Extract WhatsApp message ID from the data-id attribute for deduplication
                                    # Format: "false_PHONE@c.us_MSGID" or "true_GROUP@g.us_MSGID"
                                    # CRITICAL: Must match the JavaScript extraction in unread path (parts.slice(2).join('_'))
                                    logger.info(f"Bot {bot_profile_id}: Open chat - msg_data_id extracted: {msg_data_id[:80] if msg_data_id else 'None'}...")
                                    msg_whatsapp_id = None
                                    if msg_data_id:
                                        parts = msg_data_id.split('_')
                                        if len(parts) >= 3:
                                            # Extract chat_id from data-id if not already found
                                            for part in parts[1:]:
                                                if '@c.us' in part or '@g.us' in part:
                                                    if not chat_data_id:
                                                        chat_data_id = part
                                                    break
                                            # The message ID is everything from index 2 onwards (same as JavaScript: parts.slice(2).join('_'))
                                            msg_whatsapp_id = '_'.join(parts[2:])

                                    # Use full data-id as unique identifier (never changes, always consistent)
                                    unique_chat_id = chat_data_id or chat_name

                                    # CRITICAL: Use WhatsApp's unique message ID for deduplication
                                    # This ensures the same message is recognized regardless of which path processes it
                                    if msg_whatsapp_id:
                                        msg_id = f"{unique_chat_id}_{msg_whatsapp_id}"
                                    else:
                                        # Fallback to content-based hash if no message ID available
                                        text_sig = f"{text[:10]}_{text[-10:] if len(text) > 10 else ''}"
                                        media_sig = ''
                                        if media_info_open:
                                            media_url = media_info_open.get('mediaUrl', '')
                                            media_filename = media_info_open.get('mediaFilename', '')
                                            media_sig = f"_{hash(media_url or media_filename or '')}"
                                        msg_id = f"{unique_chat_id}_{hash(text)}_{len(text)}_{hash(text_sig)}{media_sig}"

                                    already_processed = msg_id in processed_messages

                                    # SECONDARY CHECK: For media messages WITHOUT a valid msg_whatsapp_id,
                                    # check database for recent duplicates. This catches cases where:
                                    # - data-id wasn't extracted from the DOM
                                    # - blob URL changed causing different hash each loop iteration
                                    # Only apply this check when we don't have a reliable message ID
                                    if not already_processed and not msg_whatsapp_id and media_info_open and media_info_open.get('mediaType'):
                                        try:
                                            from app.database import get_db_session, Message, Conversation
                                            # datetime and timedelta already imported at function start
                                            with get_db_session() as check_db:
                                                # Find conversation
                                                conv = check_db.query(Conversation).filter(
                                                    Conversation.bot_profile_id == bot_profile_id,
                                                    Conversation.chat_id == unique_chat_id
                                                ).first()
                                                if not conv:
                                                    conv = check_db.query(Conversation).filter(
                                                        Conversation.bot_profile_id == bot_profile_id,
                                                        Conversation.chat_name == chat_name
                                                    ).first()

                                                if conv:
                                                    # Check for media message in last 5 minutes
                                                    five_mins_ago = datetime.utcnow() - timedelta(minutes=5)
                                                    media_type = media_info_open.get('mediaType')
                                                    recent_media = check_db.query(Message).filter(
                                                        Message.conversation_id == conv.id,
                                                        Message.role == 'user',
                                                        Message.file_type != None,
                                                        Message.timestamp > five_mins_ago
                                                    ).first()

                                                    if recent_media:
                                                        already_processed = True
                                                        logger.info(f"Bot {bot_profile_id}: SKIPPING duplicate media (no msg_id) - found recent {media_type} message in last 5 mins (db_msg_id: {recent_media.id})")
                                        except Exception as db_check_err:
                                            logger.debug(f"Bot {bot_profile_id}: DB check error: {db_check_err}")

                                    if not already_processed:
                                        # Check if it's incoming (not outgoing)
                                        # WhatsApp Business uses different selectors - also check for message-out class
                                        is_outgoing = last_msg.evaluate('''el => {
                                            // Check data-testid for outgoing
                                            if (el.closest('[data-testid="conv-msg-true"]')) return true;
                                            // Check for message-out class
                                            if (el.classList.contains('message-out')) return true;
                                            if (el.closest('.message-out')) return true;
                                            return false;
                                        }''')
                                        if not is_outgoing:
                                            processed_messages.add(msg_id)
                                            is_group_detected = chat_info_open.get('isGroup', False) if chat_info_open else False

                                            # Enhanced group detection for WhatsApp Business
                                            is_group = is_group_detected or header.query_selector('[data-testid="group-icon"]') is not None

                                            # Additional group detection methods
                                            if not is_group:
                                                # Check if chat_data_id contains @g.us (group identifier)
                                                if chat_data_id and '@g.us' in chat_data_id:
                                                    is_group = True
                                                # Check for "X participants" text in header
                                                elif page.evaluate('''(header) => {
                                                    const spans = header.querySelectorAll('span');
                                                    for (const span of spans) {
                                                        const text = span.textContent || '';
                                                        if (/\\d+\\s*participant/i.test(text)) return true;
                                                    }
                                                    return false;
                                                }''', header):
                                                    is_group = True
                                                # Check for default-group icon (WhatsApp Business)
                                                elif header.query_selector('[data-icon="default-group"]'):
                                                    is_group = True
                                            # Try multiple methods for sender name extraction in group chats
                                            sender = None

                                            # Method 1: Use JavaScript to extract sender from message element
                                            sender = last_msg.evaluate('''(msg) => {
                                                // PRIORITY 1: copyable-text data attribute (most reliable)
                                                // Format: "[HH:MM, DD/MM/YYYY] Name: "
                                                const copyable = msg.querySelector('.copyable-text[data-pre-plain-text]');
                                                if (copyable) {
                                                    const preText = copyable.getAttribute('data-pre-plain-text');
                                                    const match = preText.match(/\\]\\s*([^:]+):/);
                                                    if (match && match[1].trim()) {
                                                        return match[1].trim();
                                                    }
                                                }

                                                // PRIORITY 2: Try data-testid selectors
                                                const authorEl = msg.querySelector('[data-testid="author"]') ||
                                                               msg.querySelector('[data-testid="msg-author-title"]') ||
                                                               msg.querySelector('[data-testid="author-name"]');
                                                if (authorEl && authorEl.textContent.trim()) {
                                                    return authorEl.textContent.trim();
                                                }

                                                // PRIORITY 3: Look for colored sender name span at TOP of message only
                                                // In group chats, sender name appears as first line with a color
                                                const msgRect = msg.getBoundingClientRect();
                                                const spans = msg.querySelectorAll('span[dir="auto"], span[class*="color"]');
                                                for (const span of spans) {
                                                    const text = span.textContent.trim();
                                                    // Skip empty, timestamps, very long texts
                                                    if (!text || text.length === 0 || text.length > 40) continue;
                                                    if (/^\\d{1,2}:\\d{2}/.test(text)) continue;  // Time format
                                                    if (/^\\[/.test(text)) continue;  // Media placeholder

                                                    const rect = span.getBoundingClientRect();
                                                    // Sender name MUST be at the very top of message (within 25px)
                                                    if (rect.top - msgRect.top > 25) continue;

                                                    // Check if span has non-gray color (sender names are colored)
                                                    const style = window.getComputedStyle(span);
                                                    const color = style.color;
                                                    // Must be colored (not black/white/default gray)
                                                    if (color &&
                                                        !color.includes('rgb(0, 0, 0)') &&
                                                        !color.includes('rgb(255, 255, 255)') &&
                                                        !color.includes('rgb(128, 128, 128)') &&
                                                        !color.includes('rgba(0, 0, 0')) {
                                                        // Make sure this is not inside the message text area
                                                        const copyableParent = span.closest('.copyable-text');
                                                        if (!copyableParent) {
                                                            return text;
                                                        }
                                                    }
                                                }

                                                return null;
                                            }''')

                                            # Method 2: Fallback - look for sender using Playwright selectors
                                            if not sender:
                                                sender_elem = last_msg.query_selector('[data-testid="author"]') or \
                                                             last_msg.query_selector('span[data-testid="msg-author-title"]') or \
                                                             last_msg.query_selector('[data-testid="author-name"]')
                                                if sender_elem:
                                                    sender = sender_elem.inner_text()

                                            # Download and save media if present
                                            file_info = None
                                            if media_info_open and media_info_open.get('mediaUrl'):
                                                media_url = media_info_open.get('mediaUrl')
                                                media_type = media_info_open.get('mediaType')
                                                media_filename = media_info_open.get('mediaFilename')
                                                logger.info(f"Bot {bot_profile_id}: Found media in open chat - type: {media_type}, url: {media_url[:50]}...")
                                                try:
                                                    # Handle document that needs click to download
                                                    if media_url.startswith('document-click-to-download:'):
                                                        detected_mime = media_url.split(':', 1)[1] if ':' in media_url else 'application/octet-stream'
                                                        logger.info(f"Bot {bot_profile_id}: Document in open chat needs clicking to download: {media_filename}")

                                                        # Find and click the document element using JavaScript
                                                        doc_clicked = False
                                                        doc_element_info = page.evaluate('''() => {
                                                            // Strategy 1: Find document-thumb elements (most reliable)
                                                            const docThumbs = document.querySelectorAll('[data-testid="document-thumb"], [data-testid="pdf-thumb"], [data-testid="document"]');
                                                            for (const thumb of docThumbs) {
                                                                const rect = thumb.getBoundingClientRect();
                                                                if (rect.width > 30 && rect.height > 30 && rect.x > 0 && rect.y > 0) {
                                                                    return {
                                                                        method: 'document-thumb',
                                                                        x: Math.round(rect.x + rect.width / 2),
                                                                        y: Math.round(rect.y + rect.height / 2)
                                                                    };
                                                                }
                                                            }

                                                            // Strategy 2: Find last incoming message with document
                                                            const incomingMsgs = document.querySelectorAll('.message-in');
                                                            for (let i = incomingMsgs.length - 1; i >= 0; i--) {
                                                                const msg = incomingMsgs[i];
                                                                const docEl = msg.querySelector('[data-testid="document-thumb"], [data-testid="pdf-thumb"], [role="button"]');
                                                                if (docEl) {
                                                                    const rect = docEl.getBoundingClientRect();
                                                                    if (rect.width > 30 && rect.height > 30) {
                                                                        return {
                                                                            method: 'message-in-doc',
                                                                            x: Math.round(rect.x + rect.width / 2),
                                                                            y: Math.round(rect.y + rect.height / 2)
                                                                        };
                                                                    }
                                                                }
                                                            }

                                                            return null;
                                                        }''')

                                                        try:
                                                            if doc_element_info:
                                                                logger.info(f"Bot {bot_profile_id}: Found document via {doc_element_info.get('method')} at ({doc_element_info['x']}, {doc_element_info['y']})")
                                                                page.mouse.click(doc_element_info['x'], doc_element_info['y'])
                                                                doc_clicked = True
                                                                logger.info(f"Bot {bot_profile_id}: Clicked document in open chat")
                                                                time.sleep(2)

                                                                # Open downloads page
                                                                context = page.context
                                                                downloads_page = context.new_page()
                                                                downloads_page.goto('chrome://downloads/', timeout=10000)
                                                                time.sleep(1)

                                                                # Get blob URL from downloads
                                                                download_info = downloads_page.evaluate('''async () => {
                                                                    try {
                                                                        const downloadsManager = document.querySelector('downloads-manager');
                                                                        if (!downloadsManager || !downloadsManager.shadowRoot) return null;
                                                                        const downloadItems = downloadsManager.shadowRoot.querySelectorAll('downloads-item');
                                                                        if (downloadItems.length === 0) return null;
                                                                        const firstItem = downloadItems[0];
                                                                        const itemShadow = firstItem.shadowRoot;
                                                                        if (!itemShadow) return null;
                                                                        const link = itemShadow.querySelector('a[href*="blob:"]');
                                                                        return link ? { url: link.href } : null;
                                                                    } catch(e) { return null; }
                                                                }''')

                                                                downloads_page.close()

                                                                if download_info and download_info.get('url'):
                                                                    blob_url = download_info['url']
                                                                    doc_base64 = page.evaluate('''async (blobUrl) => {
                                                                        try {
                                                                            const response = await fetch(blobUrl);
                                                                            if (!response.ok) return null;
                                                                            const blob = await response.blob();
                                                                            return new Promise((resolve) => {
                                                                                const reader = new FileReader();
                                                                                reader.onloadend = () => resolve(reader.result);
                                                                                reader.onerror = () => resolve(null);
                                                                                reader.readAsDataURL(blob);
                                                                            });
                                                                        } catch(e) { return null; }
                                                                    }''', blob_url)

                                                                    if doc_base64:
                                                                        # Fix MIME type in base64 header
                                                                        if ',' in doc_base64 and detected_mime:
                                                                            _, base64_content = doc_base64.split(',', 1)
                                                                            doc_base64 = f"data:{detected_mime};base64,{base64_content}"
                                                                        file_info = _save_incoming_media(doc_base64, 'document', bot_profile_id, chat_name, media_filename)
                                                                        if file_info:
                                                                            file_info['file_type'] = detected_mime
                                                                            logger.info(f"Bot {bot_profile_id}: Saved document from open chat: {file_info.get('file_url')}")
                                                        except Exception as doc_err:
                                                            logger.warning(f"Bot {bot_profile_id}: Failed to download document in open chat: {doc_err}")

                                                        # If download failed, store placeholder
                                                        if not file_info:
                                                            file_info = {
                                                                'file_url': None,
                                                                'file_type': detected_mime,
                                                                'file_name': media_filename or 'document',
                                                                'file_size': 0,
                                                                'is_placeholder': True
                                                            }

                                                    # Handle image that needs click to download
                                                    elif media_url.startswith('image-click-to-download:'):
                                                        detected_mime = media_url.split(':', 1)[1] if ':' in media_url else 'image/jpeg'
                                                        logger.info(f"Bot {bot_profile_id}: Image in open chat needs clicking to download")

                                                        # Click on the image to open full view
                                                        img_locator = page.locator('[data-testid="image-thumb"]').last
                                                        try:
                                                            if img_locator.is_visible(timeout=1000):
                                                                img_locator.click(timeout=3000)
                                                                logger.info(f"Bot {bot_profile_id}: Clicked image in open chat")
                                                                time.sleep(1.5)

                                                                # Get the blob URL from the full-size image
                                                                img_base64 = page.evaluate('''async () => {
                                                                    try {
                                                                        const fullImg = document.querySelector('[data-testid="media-viewer"] img') ||
                                                                                       document.querySelector('[data-testid="image-viewer"] img') ||
                                                                                       document.querySelector('div[role="dialog"] img') ||
                                                                                       document.querySelector('img[src*="blob:"]:not([data-testid="image-thumb"] img)');
                                                                        if (fullImg && fullImg.src && fullImg.src.startsWith('blob:')) {
                                                                            const response = await fetch(fullImg.src);
                                                                            if (!response.ok) return null;
                                                                            const blob = await response.blob();
                                                                            return new Promise((resolve) => {
                                                                                const reader = new FileReader();
                                                                                reader.onloadend = () => resolve(reader.result);
                                                                                reader.onerror = () => resolve(null);
                                                                                reader.readAsDataURL(blob);
                                                                            });
                                                                        }
                                                                        return null;
                                                                    } catch(e) { return null; }
                                                                }''')

                                                                # Close the viewer
                                                                try:
                                                                    page.keyboard.press('Escape')
                                                                    time.sleep(0.3)
                                                                except:
                                                                    pass

                                                                if img_base64:
                                                                    file_info = _save_incoming_media(img_base64, 'image', bot_profile_id, chat_name, media_filename)
                                                                    if file_info:
                                                                        logger.info(f"Bot {bot_profile_id}: Saved image from open chat: {file_info.get('file_url')}")
                                                        except Exception as click_err:
                                                            logger.warning(f"Bot {bot_profile_id}: Failed to click image in open chat: {click_err}")

                                                        # If download failed, store placeholder
                                                        if not file_info:
                                                            file_info = {
                                                                'file_url': None,
                                                                'file_type': detected_mime,
                                                                'file_name': media_filename or 'image',
                                                                'file_size': 0,
                                                                'is_placeholder': True
                                                            }
                                                    else:
                                                        # Download media from blob URL and convert to base64
                                                        # Retry up to 3 times with delay (blob URLs can be flaky)
                                                        media_base64 = None
                                                        for retry in range(3):
                                                            media_base64 = page.evaluate('''async (mediaUrl) => {
                                                                try {
                                                                    const response = await fetch(mediaUrl);
                                                                    if (!response.ok) return null;
                                                                    const blob = await response.blob();
                                                                    if (blob.size === 0) return null;
                                                                    return new Promise((resolve, reject) => {
                                                                        const reader = new FileReader();
                                                                        reader.onloadend = () => resolve(reader.result);
                                                                        reader.onerror = reject;
                                                                        reader.readAsDataURL(blob);
                                                                    });
                                                                } catch(e) {
                                                                    console.error('Error downloading media:', e);
                                                                    return null;
                                                                }
                                                            }''', media_url)
                                                            if media_base64:
                                                                break
                                                            if retry < 2:
                                                                logger.info(f"Bot {bot_profile_id}: Retry {retry + 1}/3 downloading media in open chat...")
                                                                time.sleep(0.5)

                                                        if media_base64:
                                                            # Check for duplicate media using content hash
                                                            content_hash = hashlib.md5(media_base64.encode()).hexdigest()
                                                            if chat_name not in saved_media_hashes:
                                                                saved_media_hashes[chat_name] = {}

                                                            if content_hash in saved_media_hashes[chat_name]:
                                                                # Duplicate - retrieve existing file_info
                                                                file_info = saved_media_hashes[chat_name][content_hash]
                                                                logger.info(f"Bot {bot_profile_id}: Using existing media for {chat_name}: {file_info.get('file_url')}")
                                                            else:
                                                                file_info = _save_incoming_media(media_base64, media_type, bot_profile_id, chat_name, media_filename)
                                                                if file_info:
                                                                    saved_media_hashes[chat_name][content_hash] = file_info
                                                                # Logging already done in _save_media
                                                        else:
                                                            logger.warning(f"Bot {bot_profile_id}: Could not download media after 3 retries in open chat from {media_url[:50]}")
                                                except Exception as media_err:
                                                    logger.error(f"Bot {bot_profile_id}: Error downloading media in open chat: {media_err}")

                                            # Clean placeholder text for media-only messages
                                            message_content = text
                                            if file_info and re.match(r'^\[(image|video|audio|document|sticker|media)\]$', text, re.IGNORECASE):
                                                message_content = ''  # No caption, just media

                                            extraction_method = chat_info_open.get('method', 'unknown') if chat_info_open else 'none'
                                            logger.info(f"Bot {bot_profile_id}: New message from {chat_name}: {text[:50]}... (has_media: {bool(file_info)})")

                                            # Extract timestamp from data-pre-plain-text for timezone detection
                                            whatsapp_timestamp = None
                                            try:
                                                whatsapp_timestamp = last_msg.evaluate('''(msg) => {
                                                    const copyable = msg.querySelector('.copyable-text[data-pre-plain-text]');
                                                    if (copyable) {
                                                        const preText = copyable.getAttribute('data-pre-plain-text') || '';
                                                        // Format: "[HH:MM AM/PM, M/D/YYYY] Name: " - extract time and date
                                                        // Support both English AM/PM and Arabic ص/م
                                                        const match = preText.match(/\\[(\\d{1,2}:\\d{2})\\s*([APMapm]{2}|ص|م),?\\s*(\\d{1,2}\\/\\d{1,2}\\/\\d{4})?\\]/);
                                                        if (match) {
                                                            return match[1] + ' ' + match[2] + (match[3] ? ', ' + match[3] : '');
                                                        }
                                                    }
                                                    return null;
                                                }''')
                                                if whatsapp_timestamp:
                                                    logger.debug(f"Bot {bot_profile_id}: Extracted WhatsApp timestamp: {whatsapp_timestamp}")
                                            except Exception as ts_err:
                                                logger.debug(f"Bot {bot_profile_id}: Error extracting timestamp: {ts_err}")

                                            _process_message_sync(
                                                page=page,
                                                ai_provider=ai_provider,
                                                config=config,
                                                bot_profile_id=bot_profile_id,
                                                chat_name=chat_name,
                                                is_group=is_group,
                                                sender=sender,
                                                message=message_content,
                                                chat_data_id=chat_data_id,
                                                file_info=file_info,
                                                whatsapp_timestamp=whatsapp_timestamp,
                                                whatsapp_message_id=msg_whatsapp_id
                                            )

                                            # Close the chat window after processing
                                            # This ensures new messages will show as unread
                                            try:
                                                page.keyboard.press('Escape')
                                                time.sleep(0.3)
                                                logger.info(f"Bot {bot_profile_id}: Closed open chat '{chat_name}' after processing")
                                            except Exception as close_err:
                                                logger.debug(f"Bot {bot_profile_id}: Could not close chat: {close_err}")
                    except Exception as e:
                        if loop_count % 15 == 1:
                            logger.debug(f"Bot {bot_profile_id}: Open chat check: {e}")

                # Process unread chats by clicking directly using Playwright
                if unread_indicators:
                    try:
                        # Get the name of currently open chat (if any) to avoid re-processing it
                        # This fixes the issue where Personal WhatsApp's unread indicators don't clear
                        current_open_chat = None
                        try:
                            current_open_chat = page.evaluate("""() => {
                                // Check if a chat is currently open by looking at the header
                                const header = document.querySelector('#main header span[title]') ||
                                              document.querySelector('[data-testid="conversation-header"] span[title]') ||
                                              document.querySelector('[data-testid="conversation-info-header"] span[title]');
                                return header ? header.getAttribute('title') : null;
                            }""")
                            if current_open_chat and loop_count % 10 == 1:
                                logger.debug(f"Bot {bot_profile_id}: Currently open chat: {current_open_chat}")
                        except:
                            pass

                        # SIMPLE APPROACH: Click on the first unread indicator's parent chat row
                        # Playwright will auto-scroll if needed
                        for indicator in unread_indicators[:1]:
                            try:
                                # Get chat name and FULL data-id from the indicator's ancestor
                                # IMPORTANT: Only look within the SAME chat row, never search entire sidebar
                                chat_info = page.evaluate('''(el) => {
                                    let name = null;
                                    let fullDataId = null;
                                    let isGroup = false;
                                    let debugInfo = { rowTag: null, rowRole: null, rowDataId: null, allDataIds: [], chatRowFound: false };

                                    // Find the chat row container that contains this unread indicator
                                    // Try multiple selectors for different WhatsApp versions
                                    let chatRow = el.closest('[role="listitem"]') ||
                                                  el.closest('[role="row"]') ||
                                                  el.closest('[data-testid="cell-frame-container"]') ||
                                                  el.closest('[data-testid="list-item"]') ||
                                                  el.closest('div[data-id]');

                                    if (!chatRow) {
                                        // Fallback: walk up to find a container with data-id or reasonable size
                                        let tempParent = el.parentElement;
                                        for (let i = 0; i < 30 && tempParent; i++) {
                                            // Check if this element has a data-id with WhatsApp format
                                            const dataId = tempParent.getAttribute('data-id');
                                            if (dataId && (dataId.includes('@c.us') || dataId.includes('@g.us'))) {
                                                chatRow = tempParent;
                                                break;
                                            }
                                            // Check for common container patterns
                                            if (tempParent.getAttribute('role') === 'listitem' ||
                                                tempParent.getAttribute('role') === 'row' ||
                                                tempParent.getAttribute('data-testid')?.includes('cell') ||
                                                tempParent.getAttribute('data-testid')?.includes('list-item')) {
                                                chatRow = tempParent;
                                                break;
                                            }
                                            tempParent = tempParent.parentElement;
                                        }
                                    }

                                    // If still no chatRow, use the sidebar itself to find data
                                    if (!chatRow) {
                                        chatRow = el.closest('#pane-side') || document.querySelector('#pane-side');
                                    }

                                    if (chatRow) {
                                        // Get chat name from WITHIN this row only
                                        // IMPORTANT: Be very specific to avoid matching message preview text
                                        // The chat name is in cell-frame-title, NOT in the message preview area
                                        let nameSpan = chatRow.querySelector('[data-testid="cell-frame-title"] span[title]');

                                        // Fallback: find first span[title] that is NOT in message preview area
                                        if (!nameSpan) {
                                            const allTitleSpans = chatRow.querySelectorAll('span[title]');
                                            for (const span of allTitleSpans) {
                                                // Skip if this span is inside a message preview container
                                                const isInMessagePreview = span.closest('[data-testid="cell-frame-secondary"]') ||
                                                                          span.closest('[data-testid="last-msg-status"]') ||
                                                                          span.closest('[data-testid="msg-time"]');
                                                if (isInMessagePreview) continue;

                                                // Skip if the title is too long (likely message preview, not a name)
                                                const title = span.getAttribute('title');
                                                if (title && title.length <= 100) {
                                                    nameSpan = span;
                                                    break;
                                                }
                                            }
                                        }

                                        if (nameSpan) {
                                            name = nameSpan.getAttribute('title');
                                        }

                                        // Get data-id from WITHIN this row only (never search ancestors)
                                        // DEBUG: Log what we find
                                        debugInfo.chatRowFound = true;
                                        debugInfo.rowTag = chatRow.tagName;
                                        debugInfo.rowRole = chatRow.getAttribute('role');
                                        debugInfo.rowDataId = chatRow.getAttribute('data-id');

                                        // Collect all data-id values in the row for debugging
                                        const allDataIdElems = chatRow.querySelectorAll('[data-id]');
                                        for (const elem of allDataIdElems) {
                                            debugInfo.allDataIds.push(elem.getAttribute('data-id'));
                                        }
                                        console.log('DEBUG chat row:', name, JSON.stringify(debugInfo));

                                        // Method 1: Check if row itself has data-id
                                        let dataId = chatRow.getAttribute('data-id');
                                        if (dataId && (dataId.includes('@c.us') || dataId.includes('@g.us'))) {
                                            fullDataId = dataId;
                                        }

                                        // Method 2: Check ANY element with data-id containing @c.us or @g.us
                                        if (!fullDataId) {
                                            for (const elem of allDataIdElems) {
                                                const id = elem.getAttribute('data-id');
                                                if (id && (id.includes('@c.us') || id.includes('@g.us'))) {
                                                    fullDataId = id;
                                                    break;
                                                }
                                            }
                                        }

                                        // Method 3: Try to find data-id in parent elements (up to listitem)
                                        if (!fullDataId) {
                                            let parent = chatRow;
                                            for (let i = 0; i < 5 && parent; i++) {
                                                const id = parent.getAttribute('data-id');
                                                if (id && (id.includes('@c.us') || id.includes('@g.us'))) {
                                                    fullDataId = id;
                                                    break;
                                                }
                                                parent = parent.parentElement;
                                            }
                                        }

                                        // Check for group icon in avatar area
                                        const avatarArea = chatRow.querySelector('[data-testid="cell-frame-primary"]') || chatRow;
                                        const hasGroupIcon = avatarArea.querySelector('[data-icon="default-group"]') !== null ||
                                                            avatarArea.querySelector('[data-testid="default-group"]') !== null;

                                        // Group detection - data-id suffix is SOURCE OF TRUTH
                                        if (fullDataId) {
                                            isGroup = fullDataId.includes('@g.us');
                                        } else {
                                            isGroup = hasGroupIcon;
                                        }
                                    }

                                    // Fallback if chatRow not found: just get name from ancestors
                                    if (!name) {
                                        let tempParent = el.parentElement;
                                        for (let i = 0; i < 20 && tempParent; i++) {
                                            const titleEl = tempParent.querySelector('[title]');
                                            if (titleEl) {
                                                name = titleEl.getAttribute('title');
                                                break;
                                            }
                                            tempParent = tempParent.parentElement;
                                        }
                                    }

                                    return {
                                        name: name || 'Unknown',
                                        fullDataId: fullDataId,
                                        isGroup: isGroup,
                                        found: !!name,
                                        debug: debugInfo || null
                                    };
                                }''', indicator)

                                chat_name = chat_info.get('name', 'Unknown')
                                chat_data_id = chat_info.get('fullDataId')
                                is_group_from_id = chat_info.get('isGroup', False)

                                # Skip if this is the currently open chat (Personal WhatsApp doesn't clear unread indicators)
                                if current_open_chat and chat_name == current_open_chat:
                                    logger.info(f"Bot {bot_profile_id}: Skipping '{chat_name}' - chat is already open")
                                    continue

                                # Log debug info to understand DOM structure
                                debug_info = chat_info.get('debug')
                                if debug_info:
                                    logger.info(f"Bot {bot_profile_id}: DOM DEBUG for '{chat_name}': rowFound={debug_info.get('chatRowFound')}, rowTag={debug_info.get('rowTag')}, rowRole={debug_info.get('rowRole')}, rowDataId={debug_info.get('rowDataId')}, allDataIds={debug_info.get('allDataIds')}")

                                # Search page for data-ids and extract phone/group info
                                if not chat_data_id:
                                    extracted_info = page.evaluate(r'''() => {
                                        // Search entire page for any data-id with WhatsApp format
                                        const elements = document.querySelectorAll('[data-id]');
                                        let phone = null;
                                        let groupId = null;
                                        let isGroup = false;
                                        const ids = [];

                                        for (const el of elements) {
                                            const id = el.getAttribute('data-id');
                                            if (id && id.includes('@c.us')) {
                                                ids.push(id);
                                                // Extract phone from format: true_971524906816@c.us_...
                                                const match = id.match(/[_]?(\d{8,15})@c\.us/);
                                                if (match && !phone) {
                                                    phone = match[1];
                                                }
                                            } else if (id && id.includes('@g.us')) {
                                                ids.push(id);
                                                isGroup = true;
                                                // Extract group ID from format: ..._120363...@g.us_...
                                                const match = id.match(/[_]?(\d+)@g\.us/);
                                                if (match && !groupId) {
                                                    groupId = match[1];
                                                }
                                            } else if (id && id.includes('@lid')) {
                                                // NEW: Handle @lid format for private chats (WhatsApp 2025+)
                                                // Format: false_34691155513450@lid_MSGID
                                                ids.push(id);
                                                const match = id.match(/[_]?(\d+)@lid/);
                                                if (match && !phone) {
                                                    phone = match[1];
                                                }
                                            }
                                        }
                                        return { phone, groupId, isGroup, ids };
                                    }''')
                                    logger.info(f"Bot {bot_profile_id}: Extracted info: phone={extracted_info.get('phone')}, groupId={extracted_info.get('groupId')}, isGroup={extracted_info.get('isGroup')}")

                                    # Use extracted info
                                    if extracted_info.get('phone'):
                                        # Check if original IDs contain @lid format
                                        ids = extracted_info.get('ids', [])
                                        if any('@lid' in str(id) for id in ids):
                                            chat_data_id = f"{extracted_info['phone']}@lid"
                                        else:
                                            chat_data_id = f"{extracted_info['phone']}@c.us"
                                        is_group_from_id = False
                                    elif extracted_info.get('groupId'):
                                        chat_data_id = f"{extracted_info['groupId']}@g.us"
                                        is_group_from_id = True
                                    elif extracted_info.get('isGroup'):
                                        is_group_from_id = True

                                    # Also check for aria-label or other attributes that might contain phone/ID
                                    contact_info = page.evaluate('''(chatName) => {
                                        // Try to find any element that might contain the chat ID
                                        const results = [];
                                        // Check for elements with the chat name
                                        const nameEls = document.querySelectorAll('[title="' + chatName + '"]');
                                        for (const el of nameEls) {
                                            let parent = el;
                                            for (let i = 0; i < 10 && parent; i++) {
                                                const dataId = parent.getAttribute('data-id');
                                                const ariaLabel = parent.getAttribute('aria-label');
                                                if (dataId) results.push({type: 'data-id', value: dataId, depth: i});
                                                if (ariaLabel && ariaLabel.length > 5) results.push({type: 'aria-label', value: ariaLabel, depth: i});
                                                parent = parent.parentElement;
                                            }
                                        }
                                        return results;
                                    }''', chat_name)
                                    if contact_info:
                                        logger.info(f"Bot {bot_profile_id}: CONTACT INFO for '{chat_name}': {contact_info}")

                                # Check cooldown - use full data-id for unique identifier
                                import time as time_module
                                current_time = time_module.time()
                                cooldown_key = chat_data_id or chat_name
                                last_processed = processed_chats_cooldown.get(cooldown_key, 0)
                                time_since_last = current_time - last_processed
                                if time_since_last < CHAT_COOLDOWN_SECONDS:
                                    # Skip this chat, it's on cooldown
                                    remaining = CHAT_COOLDOWN_SECONDS - time_since_last
                                    logger.info(f"Bot {bot_profile_id}: Skipping {chat_name} - on cooldown ({remaining:.1f}s remaining)")
                                    continue

                                logger.info(f"Bot {bot_profile_id}: Processing unread chat: {chat_name} (data_id: {chat_data_id}, isGroup: {is_group_from_id})")

                                # Use Playwright to click the chat row by title - it handles scrolling
                                title_selector = f'[title="{chat_name}"]'
                                title_el = page.query_selector(title_selector)

                                if title_el:
                                    # Click on the title element - Playwright auto-scrolls
                                    logger.info(f"Bot {bot_profile_id}: Clicking chat '{chat_name}' using Playwright")
                                    title_el.click()
                                    time.sleep(2)
                                else:
                                    # Fallback: click the indicator itself
                                    logger.info(f"Bot {bot_profile_id}: Title not found, clicking indicator directly")
                                    indicator.click()
                                    time.sleep(2)

                                # Check if conversation panel appeared using multiple selectors
                                # WhatsApp Business may use different selectors
                                panel_selectors = [
                                    '[data-testid="conversation-panel-wrapper"]',
                                    '[data-testid="conversation-compose-box-input"]',
                                    '[data-testid="conversation-panel-body"]',
                                    '[data-testid="conversation-header"]',
                                    '[data-testid="main"]',
                                    # WhatsApp Business specific selectors
                                    'footer[data-testid="compose-box"]',
                                    '[contenteditable="true"][data-tab="10"]',
                                    '[data-testid="compose-box"]',
                                    'div[title="Type a message"]',
                                    '[aria-placeholder="Type a message"]',
                                    'footer',
                                    # General selectors for message input
                                    '[contenteditable="true"]',
                                    'div[role="textbox"]'
                                ]

                                conv_panel = None
                                for selector in panel_selectors:
                                    conv_panel = page.query_selector(selector)
                                    if conv_panel:
                                        logger.info(f"Bot {bot_profile_id}: Found panel with selector: {selector}")
                                        break

                                if not conv_panel:
                                    logger.warning(f"Bot {bot_profile_id}: Chat did not open after click")
                                    # Debug: log what elements exist on the page
                                    debug_info = page.evaluate('''() => {
                                        const results = {};
                                        // Check for any contenteditable
                                        results.contenteditable = document.querySelectorAll('[contenteditable="true"]').length;
                                        // Check for footer
                                        results.footer = document.querySelectorAll('footer').length;
                                        // Check for textbox
                                        results.textbox = document.querySelectorAll('[role="textbox"]').length;
                                        // Check right panel
                                        results.rightPanel = !!document.querySelector('#main');
                                        // Get page structure
                                        results.appDiv = !!document.querySelector('#app');
                                        results.sidePanel = !!document.querySelector('#pane-side');
                                        return results;
                                    }''')
                                    logger.info(f"Bot {bot_profile_id}: Page debug: {debug_info}")
                                    continue

                                logger.info(f"Bot {bot_profile_id}: Successfully opened chat: {chat_name}")

                                # First detect if this is a group chat by checking for visual indicators
                                is_group_visual = page.query_selector('[data-testid="group-icon"]') is not None or \
                                                  page.query_selector('[data-icon="default-group"]') is not None

                                # IMPORTANT: Re-extract data-id from the page after opening the chat
                                # We search the whole page but validate against the detected chat type
                                active_chat_info = page.evaluate(r'''(isGroupVisual) => {
                                    let phone = null;
                                    let groupId = null;
                                    let isGroup = isGroupVisual;  // Start with visual detection
                                    const ids = [];
                                    let debug = { mainPanelFound: false, msgElementCount: 0, allIds: [] };

                                    // Try to find messages in the main conversation panel first
                                    const mainPanel = document.querySelector('#main') ||
                                                     document.querySelector('[data-testid="conversation-panel-messages"]');
                                    debug.mainPanelFound = !!mainPanel;

                                    // Search for data-ids - try main panel first, then fall back to page
                                    let msgElements = [];
                                    if (mainPanel) {
                                        msgElements = mainPanel.querySelectorAll('[data-id]');
                                    }

                                    // Fallback: if main panel has no data-ids, search the whole page
                                    // but only for elements that look like message containers
                                    if (msgElements.length === 0) {
                                        msgElements = document.querySelectorAll('[data-id]');
                                    }

                                    debug.msgElementCount = msgElements.length;

                                    for (const el of msgElements) {
                                        const id = el.getAttribute('data-id');
                                        if (!id) continue;

                                        debug.allIds.push(id.substring(0, 50));

                                        // Check for group IDs (@g.us)
                                        if (id.includes('@g.us')) {
                                            ids.push(id);
                                            isGroup = true;
                                            const match = id.match(/[_]?(\d+)@g\.us/);
                                            if (match && !groupId) {
                                                groupId = match[1];
                                            }
                                        }
                                        // Check for private chat IDs (@c.us)
                                        else if (id.includes('@c.us')) {
                                            ids.push(id);
                                            const match = id.match(/[_]?(\d{8,15})@c\.us/);
                                            if (match && !phone) {
                                                phone = match[1];
                                            }
                                        }
                                        // Check for linked IDs (@lid) - new WhatsApp 2025+ format for private chats
                                        else if (id.includes('@lid')) {
                                            ids.push(id);
                                            const match = id.match(/[_]?(\d+)@lid/);
                                            if (match && !phone) {
                                                phone = match[1];
                                            }
                                        }
                                    }

                                    // CRITICAL: If visual detection says it's a group, ONLY use group IDs
                                    // This prevents mixing private chat phone numbers with group chats
                                    if (isGroupVisual && groupId) {
                                        phone = null;  // Clear phone - this is a group
                                        isGroup = true;
                                    } else if (isGroupVisual && !groupId) {
                                        // Visual says group but no group ID found - still mark as group
                                        phone = null;
                                        isGroup = true;
                                    } else if (!isGroupVisual && phone) {
                                        // Visual says private and we have phone - use it
                                        groupId = null;  // Clear group ID - this is private
                                        isGroup = false;
                                    }

                                    return { phone, groupId, isGroup, ids, debug };
                                }''', is_group_visual)

                                logger.info(f"Bot {bot_profile_id}: Active chat extraction: phone={active_chat_info.get('phone')}, groupId={active_chat_info.get('groupId')}, isGroup={active_chat_info.get('isGroup')}, visualGroup={is_group_visual}")
                                debug_info = active_chat_info.get('debug', {})
                                logger.info(f"Bot {bot_profile_id}: Extraction debug: mainPanel={debug_info.get('mainPanelFound')}, elements={debug_info.get('msgElementCount')}, ids={debug_info.get('allIds', [])[:5]}")

                                # Override the previously extracted data-id with the correct one
                                if active_chat_info.get('groupId'):
                                    chat_data_id = f"{active_chat_info['groupId']}@g.us"
                                    is_group_from_id = True
                                    logger.info(f"Bot {bot_profile_id}: Using group chat_data_id: {chat_data_id}")
                                elif active_chat_info.get('phone') and not active_chat_info.get('isGroup'):
                                    # Check if original IDs contain @lid format (WhatsApp 2025+)
                                    ids = active_chat_info.get('ids', [])
                                    if any('@lid' in str(id) for id in ids):
                                        chat_data_id = f"{active_chat_info['phone']}@lid"
                                    else:
                                        chat_data_id = f"{active_chat_info['phone']}@c.us"
                                    is_group_from_id = False
                                    logger.info(f"Bot {bot_profile_id}: Using private chat_data_id: {chat_data_id}")
                                elif is_group_visual:
                                    # Visual group but no group ID found - mark as group anyway
                                    is_group_from_id = True
                                    logger.warning(f"Bot {bot_profile_id}: Visual group detected but no group ID found")

                                # IMPORTANT: Verify the correct chat is open by checking header
                                header_name = page.evaluate('''() => {
                                    const header = document.querySelector('[data-testid="conversation-info-header-chat-title"]');
                                    return header ? header.innerText : null;
                                }''')

                                if header_name and header_name != chat_name:
                                    logger.warning(f"Bot {bot_profile_id}: Chat mismatch! Expected '{chat_name}' but got '{header_name}'. Skipping.")
                                    continue

                                is_group = is_group_from_id or is_group_visual or active_chat_info.get('isGroup', False)

                                if is_group and not config.get('group_chat_enabled', True):
                                    logger.info(f"Bot {bot_profile_id}: Skipping group chat (disabled)")
                                    continue

                                # Extract contact/group profile picture from the sidebar chat list
                                # Use fetch() to avoid CORS/tainted canvas issues
                                profile_debug = page.evaluate('''async (chatName) => {
                                    const debug = {
                                        chatName: chatName,
                                        foundRows: 0,
                                        foundTitle: false,
                                        foundImg: false,
                                        imgSrc: null,
                                        base64: null,
                                        error: null
                                    };

                                    try {
                                        // Find all images with draggable=false in the sidebar
                                        const allImages = document.querySelectorAll('#pane-side img[draggable="false"]');
                                        debug.totalSidebarImages = allImages.length;

                                        // Find the chat row with matching title in the sidebar
                                        const chatRows = document.querySelectorAll('#pane-side [role="listitem"], #pane-side [role="row"], [data-testid="cell-frame-container"]');
                                        debug.foundRows = chatRows.length;

                                        let imgUrl = null;

                                        for (const row of chatRows) {
                                            // Check if this row contains our chat name
                                            const titleEl = row.querySelector('span[title], [title]');
                                            const titleValue = titleEl ? titleEl.getAttribute('title') : null;

                                            if (titleValue === chatName) {
                                                debug.foundTitle = true;
                                                // Found the chat row, now get the profile image
                                                // Try multiple selectors - groups may use different structure
                                                let img = row.querySelector('img[draggable="false"]');

                                                // If not found, try any img element
                                                if (!img) {
                                                    img = row.querySelector('img');
                                                }

                                                // Try looking in avatar containers specifically
                                                if (!img) {
                                                    const avatarDiv = row.querySelector('[data-testid="cell-frame-container"] img, [data-testid="avatar"] img, div[role="img"] img');
                                                    if (avatarDiv) img = avatarDiv;
                                                }

                                                if (img) {
                                                    debug.foundImg = true;
                                                    debug.imgSrc = img.src ? img.src.substring(0, 100) : 'no src';
                                                    debug.imgWidth = img.naturalWidth || img.width;
                                                    debug.imgHeight = img.naturalHeight || img.height;

                                                    // Accept the image if it's not a default placeholder
                                                    if (img.src && !img.src.includes('default-user') && !img.src.includes('default-group')) {
                                                        imgUrl = img.src;
                                                    }
                                                } else {
                                                    // Debug: log what elements are in the row for groups
                                                    debug.rowHTML = row.innerHTML.substring(0, 500);
                                                    debug.hasAnyImg = row.querySelectorAll('img').length;
                                                }
                                                break;
                                            }
                                        }

                                        // If we didn't find it, try alternate selectors
                                        if (!debug.foundTitle) {
                                            // Try finding by aria-selected
                                            const selected = document.querySelector('#pane-side [aria-selected="true"]');
                                            if (selected) {
                                                debug.foundSelected = true;
                                                const img = selected.querySelector('img[draggable="false"]');
                                                if (img && img.src && !img.src.includes('default-user')) {
                                                    debug.foundImg = true;
                                                    debug.imgSrc = img.src.substring(0, 100);
                                                    imgUrl = img.src;
                                                }
                                            }
                                        }

                                        // Fetch the image and convert to base64 (avoids CORS canvas issues)
                                        if (imgUrl) {
                                            try {
                                                const response = await fetch(imgUrl);
                                                const blob = await response.blob();
                                                const base64 = await new Promise((resolve, reject) => {
                                                    const reader = new FileReader();
                                                    reader.onloadend = () => resolve(reader.result);
                                                    reader.onerror = reject;
                                                    reader.readAsDataURL(blob);
                                                });
                                                debug.base64 = base64;
                                            } catch(e) {
                                                debug.fetchError = e.toString();
                                            }
                                        }

                                    } catch(e) {
                                        debug.error = e.toString();
                                    }

                                    return debug;
                                }''', chat_name)

                                logger.info(f"Bot {bot_profile_id}: Profile pic extraction for '{chat_name}':")
                                logger.info(f"Bot {bot_profile_id}:   - foundRows: {profile_debug.get('foundRows')}")
                                logger.info(f"Bot {bot_profile_id}:   - foundTitle: {profile_debug.get('foundTitle')}")
                                logger.info(f"Bot {bot_profile_id}:   - foundImg: {profile_debug.get('foundImg')}")
                                logger.info(f"Bot {bot_profile_id}:   - imgSrc: {profile_debug.get('imgSrc')}")
                                logger.info(f"Bot {bot_profile_id}:   - hasBase64: {bool(profile_debug.get('base64'))}")
                                if profile_debug.get('hasAnyImg') is not None:
                                    logger.info(f"Bot {bot_profile_id}:   - hasAnyImg in row: {profile_debug.get('hasAnyImg')}")
                                if profile_debug.get('rowHTML'):
                                    logger.debug(f"Bot {bot_profile_id}:   - rowHTML: {profile_debug.get('rowHTML')[:200]}")
                                if profile_debug.get('error'):
                                    logger.error(f"Bot {bot_profile_id}:   - error: {profile_debug.get('error')}")
                                if profile_debug.get('fetchError'):
                                    logger.error(f"Bot {bot_profile_id}:   - fetchError: {profile_debug.get('fetchError')}")

                                contact_profile_pic = profile_debug.get('base64') if profile_debug else None
                                if contact_profile_pic:
                                    logger.info(f"Bot {bot_profile_id}: Successfully extracted profile pic for '{chat_name}' (length: {len(contact_profile_pic)})")
                                else:
                                    logger.warning(f"Bot {bot_profile_id}: Could not extract profile pic for '{chat_name}'")

                                # Get messages from the conversation - WhatsApp Business compatible
                                messages_data = page.evaluate(r'''() => {
                                    const result = [];

                                    // Find message containers - multiple approaches for WhatsApp Business
                                    // Method 1: Look for message rows in the main panel
                                    let msgRows = document.querySelectorAll('[data-testid="msg-container"]');

                                    // Method 2: WhatsApp Business uses different structure
                                    if (msgRows.length === 0) {
                                        msgRows = document.querySelectorAll('[data-id]');
                                        msgRows = Array.from(msgRows).filter(el =>
                                            el.getAttribute('data-id') &&
                                            el.getAttribute('data-id').includes('@')
                                        );
                                    }

                                    // Method 3: Find by class patterns
                                    if (msgRows.length === 0) {
                                        msgRows = document.querySelectorAll('.message-in, .message-out');
                                    }

                                    // Method 4: Find copyable-text elements (message text containers)
                                    if (msgRows.length === 0) {
                                        const copyableTexts = document.querySelectorAll('.copyable-text');
                                        msgRows = Array.from(copyableTexts).map(el => el.closest('[class*="message"]') || el.parentElement.parentElement);
                                        msgRows = msgRows.filter(el => el !== null);
                                    }

                                    // Method 5: Look for spans with selectable-text class
                                    if (msgRows.length === 0) {
                                        const selectableSpans = document.querySelectorAll('span.selectable-text');
                                        if (selectableSpans.length > 0) {
                                            // Get parent message containers
                                            msgRows = Array.from(selectableSpans).map(span => {
                                                let parent = span.parentElement;
                                                for (let i = 0; i < 10 && parent; i++) {
                                                    if (parent.getAttribute && (
                                                        parent.getAttribute('data-id') ||
                                                        parent.classList.contains('message-in') ||
                                                        parent.classList.contains('message-out')
                                                    )) {
                                                        return parent;
                                                    }
                                                    parent = parent.parentElement;
                                                }
                                                return span.parentElement.parentElement;
                                            });
                                        }
                                    }

                                    console.log('Found ' + msgRows.length + ' message rows');

                                    // Get last 5 messages
                                    const lastMsgs = Array.from(msgRows).slice(-5);

                                    lastMsgs.forEach(msg => {
                                        if (!msg) return;

                                        let text = '';
                                        let mediaUrl = null;
                                        let mediaType = null;
                                        let mediaFilename = null;

                                        // First, check for media content
                                        // IMPORTANT: Check IMAGES FIRST because document selectors can be too broad

                                        // IMAGE detection FIRST - these are very specific selectors
                                        const imageThumb = msg.querySelector('[data-testid="image-thumb"]') ||
                                                          msg.querySelector('[data-testid="media-state-photo"]') ||
                                                          msg.querySelector('div[role="button"][class*="image"]');
                                        const imageImg = msg.querySelector('[data-testid="image-thumb"] img') ||
                                                        msg.querySelector('img[data-testid="media-canvas"]') ||
                                                        msg.querySelector('div[role="button"] img[src*="blob:"]');

                                        // If we found an image indicator, mark as image and skip document detection
                                        let isDefinitelyImage = false;
                                        if (imageThumb || (imageImg && imageImg.src && !imageImg.src.includes('pps.whatsapp.net'))) {
                                            isDefinitelyImage = true;
                                            if (imageImg && imageImg.src && imageImg.src.startsWith('blob:')) {
                                                mediaUrl = imageImg.src;
                                                mediaType = 'image';
                                                console.log('EARLY IMAGE: Found image with blob URL:', mediaUrl.substring(0, 50));
                                            } else {
                                                mediaUrl = 'image-click-to-download:image/jpeg';
                                                mediaType = 'image';
                                                console.log('EARLY IMAGE: Found image that needs clicking');
                                            }
                                        }

                                        // Document detection - ONLY if not an image
                                        // Use STRICT selectors - removed overly broad ones that could match images
                                            const docSelectors = [
                                                '[data-testid="document-thumb"]',
                                                '[data-testid="document"]',
                                                '[data-testid="pdf-thumb"]',
                                                '[data-testid="media-state-document"]',
                                                '[data-testid="file-bubble"]',
                                                '[data-testid="doc-message"]',
                                                '[data-testid="document-bubble"]',
                                                '[data-testid="document-message"]',
                                                // Group chat specific selectors - REMOVED media-url-provider (too generic)
                                                '[data-testid="msg-document"]',
                                                '[data-testid="audio-document"]',
                                                // Icon-based selectors - document-specific icons only
                                                'div[data-icon="document"]',
                                                'span[data-icon="audio-file"]',
                                                'span[data-icon="document"]',
                                                // File extension icons that indicate documents
                                                'span[data-icon="pdf"]',
                                                'span[data-icon="doc"]',
                                                'span[data-icon="xls"]',
                                                'span[data-icon="ppt"]',
                                                'span[data-icon="txt"]',
                                                'span[data-icon="zip"]'
                                                // REMOVED: overly broad selectors that could match images
                                                // - '[data-testid="media-url-provider"]' - matches all media
                                                // - '[role="button"][aria-label*="Download"]' - exists on images
                                                // - 'span[dir="ltr"]:not([class*="selectable"])' - too generic
                                                // - div[class*="document/doc/file"] - can false positive
                                            ];

                                            let docContainer = null;
                                            let downloadLink = null;
                                            let hasFileExtension = false;
                                            let detectedFilenameFromText = null;

                                            // SKIP document detection entirely if we already found an image
                                            if (!isDefinitelyImage) {
                                                for (const selector of docSelectors) {
                                                    try {
                                                        docContainer = msg.querySelector(selector);
                                                        if (docContainer) {
                                                            console.log('Found document via selector:', selector);
                                                            break;
                                                        }
                                                    } catch (e) { /* selector may be invalid */ }
                                                }

                                                // Also check if the message has a download link (specific to documents)
                                                downloadLink = msg.querySelector('a[download]');

                                                // ENHANCED: Check for file extension patterns in ANY text within the message
                                                // This helps detect documents in group chats where DOM structure may differ
                                                if (!docContainer && !downloadLink) {
                                                    // Get all text content from the message
                                                    const allText = msg.innerText || msg.textContent || '';
                                                    // Look for common document extensions
                                                    const fileExtRegex = /([^\s<>"']+\.(pdf|docx?|xlsx?|pptx?|csv|txt|zip|rar|json|xml))/i;
                                                    const match = allText.match(fileExtRegex);
                                                    if (match) {
                                                        hasFileExtension = true;
                                                        detectedFilenameFromText = match[1].trim();
                                                        console.log('ENHANCED: Found file extension in text:', detectedFilenameFromText);
                                                    }

                                                    // Also check title attributes which often contain filenames
                                                    const elementsWithTitle = msg.querySelectorAll('[title]');
                                                    for (const el of elementsWithTitle) {
                                                        const title = el.getAttribute('title') || '';
                                                        const titleMatch = title.match(fileExtRegex);
                                                        if (titleMatch) {
                                                            hasFileExtension = true;
                                                            detectedFilenameFromText = titleMatch[1].trim();
                                                            docContainer = el;  // Use this element as document container
                                                            console.log('ENHANCED: Found file in title attribute:', detectedFilenameFromText);
                                                            break;
                                                        }
                                                    }
                                                }
                                            }

                                            // Only proceed if we found a specific document indicator (and not already an image)
                                            if (!isDefinitelyImage && (docContainer || downloadLink || hasFileExtension)) {
                                                // Try to find a download link or blob URL
                                                const docLink = msg.querySelector('a[href*="blob:"]') ||
                                                               msg.querySelector('a[download]');
                                                if (docLink) {
                                                    mediaUrl = docLink.href;
                                                }

                                                // Get filename from specific document-related elements only
                                                const filenameSelectors = [
                                                    '[data-testid="document-name"]',
                                                    '[data-testid="filename"]',
                                                    '[data-testid="document-title"]',
                                                    '[data-testid="file-name"]',
                                                    '[data-testid="doc-filename"]',
                                                    'span[title*=".pdf"]',
                                                    'span[title*=".doc"]',
                                                    'span[title*=".xls"]',
                                                    'span[title*=".ppt"]',
                                                    'span[title*=".csv"]',
                                                    'span[title*=".txt"]',
                                                    'div[title*=".pdf"]',
                                                    'div[title*=".doc"]'
                                                ];

                                                for (const selector of filenameSelectors) {
                                                    const el = msg.querySelector(selector);
                                                    if (el) {
                                                        const fname = el.getAttribute('title') || el.innerText || el.textContent;
                                                        if (fname && fname.trim()) {
                                                            mediaFilename = fname.trim();
                                                            // Clean filename: extract from 'Download "filename.ext"' format
                                                            const downloadMatch = mediaFilename.match(/Download\s+"([^"]+)"/);
                                                            if (downloadMatch) {
                                                                mediaFilename = downloadMatch[1];
                                                            } else {
                                                                // Remove leading/trailing quotes and whitespace
                                                                mediaFilename = mediaFilename.replace(/^["'\s]+|["'\s]+$/g, '');
                                                            }
                                                            break;
                                                        }
                                                    }
                                                }

                                                // Use detected filename from text if no other filename found
                                                if (!mediaFilename && detectedFilenameFromText) {
                                                    mediaFilename = detectedFilenameFromText;
                                                    console.log('Using filename from text detection:', mediaFilename);
                                                }

                                                // Mark as document if we found a container OR detected file extension
                                                if (docContainer || hasFileExtension) {
                                                    mediaType = 'document';
                                                    // If no blob URL available, mark for click-to-download
                                                    if (!mediaUrl || !mediaUrl.startsWith('blob:')) {
                                                        // Derive MIME type from filename if available
                                                        let docMime = 'application/octet-stream';
                                                        if (mediaFilename) {
                                                            // Extract extension and clean non-alphanumeric chars
                                                            const docExt = mediaFilename.split('.').pop().toLowerCase().replace(/[^a-z0-9]/g, '');
                                                            const docMimeTypes = {
                                                                'pdf': 'application/pdf',
                                                                'doc': 'application/msword',
                                                                'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                                                                'xls': 'application/vnd.ms-excel',
                                                                'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                                                'ppt': 'application/vnd.ms-powerpoint',
                                                                'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                                                                'csv': 'text/csv',
                                                                'txt': 'text/plain',
                                                                'zip': 'application/zip',
                                                                'rar': 'application/x-rar-compressed'
                                                            };
                                                            docMime = docMimeTypes[docExt] || docMime;
                                                        }
                                                        // Use document-click-to-download to trigger click and download
                                                        mediaUrl = 'document-click-to-download:' + docMime;
                                                    }
                                                    console.log('Found document:', mediaFilename || 'unknown', mediaUrl?.substring(0, 50) || 'no-url', hasFileExtension ? '(via text detection)' : '');
                                                }
                                            }

                                        // Image detection - FALLBACK (primary detection is done at the start)
                                        // This catches any images that weren't detected by the early check
                                        if (!mediaUrl && !isDefinitelyImage) {
                                            const imageContainer = msg.querySelector('[data-testid="image-thumb"]') ||
                                                                  msg.querySelector('[data-testid="media-state-photo"]') ||
                                                                  msg.querySelector('div[role="button"][class*="image"]');
                                            const imageThumb = msg.querySelector('[data-testid="image-thumb"] img') ||
                                                              msg.querySelector('img[data-testid="media-canvas"]') ||
                                                              msg.querySelector('[data-testid="media-url-provider"] img') ||
                                                              msg.querySelector('div[role="button"] img[src*="blob:"]');
                                            if (imageThumb && imageThumb.src && !imageThumb.src.includes('pps.whatsapp.net')) {
                                                // Check if it's a blob URL (directly downloadable) or needs clicking
                                                if (imageThumb.src.startsWith('blob:')) {
                                                    mediaUrl = imageThumb.src;
                                                    mediaType = 'image';
                                                    console.log('Found image with blob URL:', mediaUrl.substring(0, 50));
                                                } else {
                                                    // Image needs to be clicked to get full-size blob URL
                                                    mediaUrl = 'image-click-to-download:image/jpeg';
                                                    mediaType = 'image';
                                                    console.log('Found image that needs clicking to download');
                                                }
                                            } else if (imageContainer && !imageThumb) {
                                                // Image container exists but no img element yet - needs clicking
                                                mediaUrl = 'image-click-to-download:image/jpeg';
                                                mediaType = 'image';
                                                console.log('Found image container without loaded image - needs clicking');
                                            }
                                        }

                                        // Video detection
                                        if (!mediaUrl) {
                                            const videoThumb = msg.querySelector('[data-testid="video-thumb"] img') ||
                                                              msg.querySelector('video source') ||
                                                              msg.querySelector('video');
                                            if (videoThumb) {
                                                mediaUrl = videoThumb.src || videoThumb.querySelector('source')?.src;
                                                mediaType = 'video';
                                                console.log('Found video:', mediaUrl?.substring(0, 50));
                                            }
                                        }

                                        // Sticker detection - STRICT selectors only (data-testid based)
                                        if (!mediaUrl) {
                                            const stickerSelectors = [
                                                '[data-testid="sticker"] img',
                                                'img[data-testid="sticker"]',
                                                '[data-testid="x-sticker"] img',
                                                '[data-testid="media-state-sticker"] img',
                                                '[data-testid="sticker-image"] img'
                                            ];
                                            for (const selector of stickerSelectors) {
                                                const sticker = msg.querySelector(selector);
                                                if (sticker && sticker.src &&
                                                    !sticker.src.includes('pps.whatsapp.net')) {
                                                    mediaUrl = sticker.src;
                                                    mediaType = 'sticker';
                                                    console.log('Found sticker via ' + selector + ':', mediaUrl.substring(0, 50));
                                                    break;
                                                }
                                            }
                                        }

                                        // Animated emoji detection - STRICT selectors only
                                        if (!mediaUrl) {
                                            const emojiSelectors = [
                                                '[data-testid="animated-emoji"] img',
                                                'img[data-testid="animated-emoji"]',
                                                '[data-testid="big-emoji"] img'
                                            ];
                                            for (const selector of emojiSelectors) {
                                                const emoji = msg.querySelector(selector);
                                                if (emoji && emoji.src) {
                                                    mediaUrl = emoji.src;
                                                    mediaType = 'sticker';
                                                    console.log('Found animated emoji via ' + selector + ':', mediaUrl.substring(0, 50));
                                                    break;
                                                }
                                            }
                                        }

                                        // Big emoji text detection (single emoji messages shown as large text)
                                        if (!mediaUrl && !text) {
                                            const emojiText = msg.querySelector('[data-testid="big-emoji"]');
                                            if (emojiText) {
                                                text = emojiText.innerText || '';
                                                console.log('Found big emoji text:', text);
                                            }
                                        }

                                        // Audio/Voice message detection
                                        if (!mediaUrl) {
                                            const audioEl = msg.querySelector('audio') ||
                                                           msg.querySelector('[data-testid="audio-play"]') ||
                                                           msg.querySelector('[data-testid="ptt"]');
                                            if (audioEl) {
                                                const audioSource = audioEl.querySelector('source') || audioEl;
                                                if (audioSource.src) {
                                                    mediaUrl = audioSource.src;
                                                    mediaType = 'audio';
                                                    console.log('Found audio:', mediaUrl.substring(0, 50));
                                                }
                                            }
                                        }

                                        // For MEDIA messages with captions, try specific caption selectors first
                                        if (mediaUrl || mediaType) {
                                            // Method 0: Try caption-specific selectors for media messages
                                            const captionSelectors = [
                                                '[data-testid="media-caption"] span.selectable-text',
                                                '[data-testid="media-caption"]',
                                                '.copyable-text[data-pre-plain-text]',  // Caption often has this attribute
                                            ];
                                            for (const selector of captionSelectors) {
                                                const captionEl = msg.querySelector(selector);
                                                if (captionEl) {
                                                    // For copyable-text, get only direct text, not including sender info
                                                    const captionText = captionEl.innerText || captionEl.textContent || '';
                                                    if (captionText.trim()) {
                                                        text = captionText.trim();
                                                        console.log('Found media caption via', selector, ':', text.substring(0, 30));
                                                        break;
                                                    }
                                                }
                                            }
                                        }

                                        // Method 1: span.selectable-text (most reliable for text messages)
                                        if (!text) {
                                            const selectableText = msg.querySelector('span.selectable-text');
                                            if (selectableText) {
                                                text = selectableText.innerText || selectableText.textContent || '';
                                            }
                                        }

                                        // Method 2: copyable-text
                                        if (!text) {
                                            const copyableText = msg.querySelector('.copyable-text');
                                            if (copyableText) {
                                                text = copyableText.innerText || copyableText.textContent || '';
                                            }
                                        }

                                        // Method 3: data-testid="msg-text" (regular WhatsApp)
                                        if (!text) {
                                            const msgText = msg.querySelector('[data-testid="msg-text"]');
                                            if (msgText) {
                                                text = msgText.innerText || msgText.textContent || '';
                                            }
                                        }

                                        // Method 4: Any span with text (but exclude sender name elements)
                                        if (!text && !mediaUrl) {
                                            // Only use this fallback if there's no media
                                            // For media messages, we want to use placeholder, not random spans
                                            const spans = msg.querySelectorAll('span');
                                            for (const span of spans) {
                                                // Skip sender name elements
                                                if (span.closest('[data-testid="author"]') ||
                                                    span.closest('[data-testid="msg-author-title"]') ||
                                                    span.closest('[data-testid="author-name"]') ||
                                                    span.hasAttribute('data-testid') && span.getAttribute('data-testid').includes('author')) {
                                                    continue;
                                                }
                                                const t = span.innerText || '';
                                                if (t.length > 2 && !t.includes(':') && !t.match(/^\d{1,2}:\d{2}/)) {
                                                    text = t;
                                                    break;
                                                }
                                            }
                                        }

                                        text = text.trim();

                                        // CLEAN TEXT: For ALL media messages, remove timestamps that get captured
                                        // Also remove sender name, phone for group messages
                                        // Check if group message by looking at data-id attribute
                                        const dataIdForClean = msg.getAttribute('data-id') || '';
                                        const isGroupMsgForClean = dataIdForClean.includes('@g.us');
                                        if ((mediaUrl || mediaType) && text) {
                                            const lines = text.split('\\n').map(l => l.trim()).filter(l => l);
                                            const cleanLines = [];
                                            for (const line of lines) {
                                                // Skip if line matches timestamp pattern (e.g., "8:21 AM", "14:30")
                                                if (/^\\d{1,2}:\\d{2}(\\s*(AM|PM))?$/i.test(line)) continue;
                                                // Skip if line matches phone number pattern (e.g., "+60 16-260 9676")
                                                if (isGroupMsgForClean && /^\\+?\\d[\\d\\s\\-()]{6,}$/.test(line)) continue;
                                                // Skip common metadata patterns
                                                if (/^(Yesterday|Today|\\d{1,2}\\/\\d{1,2}\\/\\d{2,4})$/i.test(line)) continue;
                                                // Skip short single-word lines at start (likely sender name)
                                                if (isGroupMsgForClean && cleanLines.length === 0 && /^[A-Z][a-z]+$/.test(line) && line.length < 20) continue;
                                                cleanLines.push(line);
                                            }
                                            text = cleanLines.join('\\n').trim();
                                            if (text !== lines.join('\\n').trim()) {
                                                console.log('Cleaned media caption text, removed metadata');
                                            }
                                        }

                                        // FALLBACK: Detect document by filename in text content
                                        // If text looks like a filename and no media detected, try to find document
                                        if (text && !mediaUrl) {
                                            const docExtensions = /\.(pdf|docx?|xlsx?|pptx?|csv|txt|zip|rar|json|xml|html?|md|rtf)["'\s]*$/i;
                                            if (docExtensions.test(text.trim())) {
                                                let detectedFilename = text.trim();
                                                // Clean filename: extract from 'Download "filename.ext"' format
                                                const downloadMatch = detectedFilename.match(/Download\s+"([^"]+)"/);
                                                if (downloadMatch) {
                                                    detectedFilename = downloadMatch[1];
                                                } else {
                                                    // Remove leading/trailing quotes and whitespace
                                                    detectedFilename = detectedFilename.replace(/^["'\s]+|["'\s]+$/g, '');
                                                }
                                                console.log('FALLBACK: Detected document filename in text:', detectedFilename);
                                                mediaFilename = detectedFilename;
                                                mediaType = 'document';

                                                // Determine MIME type from extension (clean non-alphanumeric chars)
                                                const ext = detectedFilename.split('.').pop().toLowerCase().replace(/[^a-z0-9]/g, '');
                                                const mimeTypes = {
                                                    'pdf': 'application/pdf',
                                                    'doc': 'application/msword',
                                                    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                                                    'xls': 'application/vnd.ms-excel',
                                                    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                                    'ppt': 'application/vnd.ms-powerpoint',
                                                    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                                                    'csv': 'text/csv',
                                                    'txt': 'text/plain',
                                                    'zip': 'application/zip',
                                                    'rar': 'application/x-rar-compressed',
                                                    'json': 'application/json',
                                                    'xml': 'application/xml',
                                                    'html': 'text/html',
                                                    'htm': 'text/html',
                                                    'md': 'text/markdown',
                                                    'rtf': 'application/rtf'
                                                };
                                                const detectedMime = mimeTypes[ext] || 'application/octet-stream';

                                                // Try additional selectors for document download
                                                const docDownloadSelectors = [
                                                    'a[href*="blob:"]',
                                                    'a[download]',
                                                    '[role="button"][title*=".pdf"]',
                                                    '[role="button"][title*=".doc"]',
                                                    '[role="button"][title*=".xls"]',
                                                    'div[data-testid="media-url-provider"] a',
                                                    'div[role="button"]'
                                                ];

                                                for (const selector of docDownloadSelectors) {
                                                    const el = msg.querySelector(selector);
                                                    if (el) {
                                                        const href = el.href || el.getAttribute('href');
                                                        if (href && href.startsWith('blob:')) {
                                                            mediaUrl = href;
                                                            console.log('Found document blob URL via fallback:', mediaUrl.substring(0, 50));
                                                            break;
                                                        }
                                                    }
                                                }

                                                // If still no URL, mark for click-to-download
                                                // The actual click will be handled in Python after we collect all messages
                                                if (!mediaUrl) {
                                                    // Store info for later click-to-download attempt
                                                    mediaUrl = 'document-click-to-download:' + detectedMime;
                                                    console.log('Document needs click to download. Filename:', mediaFilename, 'MIME:', detectedMime);
                                                }

                                                // Clear the text since it's just the filename
                                                text = '';
                                            }
                                        }

                                        // If no text but has media, use placeholder
                                        if (!text && mediaUrl) {
                                            text = `[${mediaType || 'media'}]`;
                                        }

                                        // Skip if no text and no media
                                        if (!text && !mediaUrl) return;

                                        // Check if outgoing - WhatsApp Business uses data-testid="conv-msg-true"
                                        const isOutgoing = msg.classList.contains('message-out') ||
                                                          msg.closest('.message-out') !== null ||
                                                          msg.closest('[data-testid="conv-msg-true"]') !== null ||
                                                          msg.querySelector('[data-testid="msg-dblcheck"]') !== null ||
                                                          msg.querySelector('[data-testid="msg-check"]') !== null ||
                                                          msg.querySelector('[data-icon="msg-dblcheck"]') !== null ||
                                                          msg.querySelector('[data-icon="msg-check"]') !== null;

                                        // Get sender name - try multiple selectors for WhatsApp Web compatibility
                                        // This works for both text messages AND media messages (images/documents)
                                        let sender = null;

                                        // Method 1: Standard author elements
                                        const authorEl = msg.querySelector('[data-testid="author"]') ||
                                                        msg.querySelector('[data-testid="msg-author-title"]') ||
                                                        msg.querySelector('[data-testid="author-name"]') ||
                                                        msg.querySelector('span[dir="auto"][aria-label]');
                                        if (authorEl) {
                                            sender = authorEl.innerText || authorEl.getAttribute('aria-label') || '';
                                        }

                                        // Method 2: Extract from data-pre-plain-text attribute (format: "[HH:MM AM/PM, DD/MM/YYYY] Name: ")
                                        // This works for text messages - also extract timestamp for timezone detection
                                        let whatsappTimestamp = null;
                                        if (!sender) {
                                            const copyableText = msg.querySelector('.copyable-text[data-pre-plain-text]');
                                            if (copyableText) {
                                                const preText = copyableText.getAttribute('data-pre-plain-text') || '';
                                                const match = preText.match(/\]\s*([^:]+):/);
                                                if (match) {
                                                    sender = match[1].trim();
                                                }
                                                // Extract timestamp: [HH:MM AM/PM, M/D/YYYY] - supports English and Arabic AM/PM
                                                const tsMatch = preText.match(/\[(\d{1,2}:\d{2})\s*([APMapm]{2}|ص|م),?\s*(\d{1,2}\/\d{1,2}\/\d{4})?\]/);
                                                if (tsMatch) {
                                                    whatsappTimestamp = tsMatch[1] + ' ' + tsMatch[2] + (tsMatch[3] ? ', ' + tsMatch[3] : '');
                                                }
                                            }
                                        } else {
                                            // Still try to get timestamp even if sender was found by other method
                                            const copyableText = msg.querySelector('.copyable-text[data-pre-plain-text]');
                                            if (copyableText) {
                                                const preText = copyableText.getAttribute('data-pre-plain-text') || '';
                                                const tsMatch = preText.match(/\[(\d{1,2}:\d{2})\s*([APMapm]{2}|ص|م),?\s*(\d{1,2}\/\d{1,2}\/\d{4})?\]/);
                                                if (tsMatch) {
                                                    whatsappTimestamp = tsMatch[1] + ' ' + tsMatch[2] + (tsMatch[3] ? ', ' + tsMatch[3] : '');
                                                }
                                            }
                                        }

                                        // Method 3: For MEDIA messages (images/documents) in groups
                                        // The sender name is displayed as a colored span at the top of the message
                                        // Look for spans with specific styling (colored text for sender names)
                                        if (!sender) {
                                            // Get the message container
                                            const msgContainer = msg.closest('[data-testid="msg-container"]') || msg;

                                            // Look for colored sender name spans (WhatsApp uses different colors for different senders)
                                            // These are typically the first span with a specific color style
                                            const allSpans = msgContainer.querySelectorAll('span[dir="auto"]');
                                            for (const span of allSpans) {
                                                const style = window.getComputedStyle(span);
                                                const color = style.color;
                                                const text = (span.innerText || '').trim();

                                                // Check if this span has a colored text (not black/white/gray) and contains a name
                                                // Sender names are short (typically < 30 chars) and don't contain newlines
                                                if (text && text.length > 0 && text.length < 30) {
                                                    // Check if it's a colored span (sender names have unique colors in groups)
                                                    if (color && !color.includes('rgb(255, 255, 255)') &&
                                                        !color.includes('rgba(255, 255, 255') &&
                                                        !color.includes('rgb(0, 0, 0)') &&
                                                        !color.includes('rgba(0, 0, 0')) {
                                                        // Verify it's at the top of the message (sender names are above content)
                                                        const spanRect = span.getBoundingClientRect();
                                                        const msgRect = msgContainer.getBoundingClientRect();
                                                        if (spanRect.top - msgRect.top < 50) {
                                                            // Exclude common non-name texts
                                                            if (!text.match(/^\d+:\d+/) && // Not a timestamp
                                                                !text.match(/^[\d,.]+ [KMG]?B$/i) && // Not a file size
                                                                !text.match(/^\d+ page/i) && // Not page count
                                                                text !== 'PDF' && text !== 'DOC' && text !== 'XLS') {
                                                                sender = text;
                                                                break;
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }

                                        // Method 4: Check for sender in message row's context (previous sibling or parent)
                                        if (!sender) {
                                            const msgRow = msg.closest('[role="row"]') || msg.parentElement;
                                            if (msgRow) {
                                                // Look for author element in the row context
                                                const rowAuthor = msgRow.querySelector('[data-testid="author"]') ||
                                                                 msgRow.querySelector('[data-testid="msg-author-title"]');
                                                if (rowAuthor) {
                                                    sender = rowAuthor.innerText || '';
                                                }
                                            }
                                        }

                                        // Method 5: Check aria-label on the message container which sometimes contains sender info
                                        if (!sender) {
                                            const ariaLabel = msg.getAttribute('aria-label') || '';
                                            // Format might be "Message from SenderName"
                                            const ariaMatch = ariaLabel.match(/from\s+([^,:.]+)/i);
                                            if (ariaMatch) {
                                                sender = ariaMatch[1].trim();
                                            }
                                        }

                                        // Get sender profile picture URL (for group messages)
                                        let senderProfilePic = null;
                                        let avatarDebug = { msgRowFound: false, allImgCount: 0, allImgSrcs: [], method: null };

                                        // In WhatsApp Web group chats, avatar is in a sibling/adjacent element
                                        // The structure is: [avatar container] [message bubble]
                                        // IMPORTANT: WhatsApp only shows avatars for the first message in a cluster
                                        // from the same sender. We need to look at previous messages for the avatar.
                                        const msgRow = msg.closest('[data-testid="msg-container"]') ||
                                                      msg.closest('[role="row"]') ||
                                                      msg.closest('.message-in') ||
                                                      msg.parentElement;

                                        if (msgRow) {
                                            avatarDebug.msgRowFound = true;
                                            avatarDebug.msgRowTag = msgRow.tagName;
                                            // Debug: capture all images in msgRow
                                            const allDebugImgs = msgRow.querySelectorAll('img');
                                            avatarDebug.allImgCount = allDebugImgs.length;
                                            allDebugImgs.forEach(img => {
                                                if (img.src) avatarDebug.allImgSrcs.push({ src: img.src.substring(0, 100), w: img.width, h: img.height });
                                            });

                                            // Helper function to check if image is likely a profile pic
                                            const isProfilePic = (img) => {
                                                if (!img || !img.src) return false;
                                                const src = img.src;
                                                // Profile pics are from pps.whatsapp.net OR are small blob images
                                                if (src.includes('pps.whatsapp.net') || src.includes('web.whatsapp.com/pp')) return true;
                                                // Blob images that are small (profile pics are typically 28-50px)
                                                if (src.includes('blob:') && img.width > 20 && img.width < 80 && img.height > 20 && img.height < 80) return true;
                                                return false;
                                            };

                                            // Try to find avatar in the message row or its siblings
                                            const avatarSelectors = [
                                                // WhatsApp Web 2025/2026 selectors
                                                'img[data-testid="author-avatar"]',
                                                '[data-testid="contact-avatar"] img',
                                                '[data-testid="user-avatar"] img',
                                                // Avatar button container
                                                'div[role="button"] img[src*="pps.whatsapp.net"]',
                                                'div[role="button"] img[src*="blob:"]',
                                                // Generic avatar selectors
                                                'img[src*="pps.whatsapp.net"]',
                                                'img[src*="web.whatsapp.com/pp"]'
                                            ];

                                            // First check within the message row using specific selectors
                                            for (const selector of avatarSelectors) {
                                                const avatar = msgRow.querySelector(selector);
                                                if (isProfilePic(avatar)) {
                                                    senderProfilePic = avatar.src;
                                                    avatarDebug.method = 'msgRow-selector';
                                                    break;
                                                }
                                            }

                                            // Check previous siblings (avatar is at the start of a message cluster)
                                            if (!senderProfilePic) {
                                                let sibling = msgRow.previousElementSibling;
                                                let siblingCount = 0;
                                                // Check up to 10 previous siblings to find the avatar
                                                while (sibling && siblingCount < 10) {
                                                    // Try specific selectors first
                                                    for (const selector of avatarSelectors) {
                                                        const avatar = sibling.querySelector(selector);
                                                        if (isProfilePic(avatar)) {
                                                            senderProfilePic = avatar.src;
                                                            avatarDebug.method = 'prevSiblings-' + siblingCount;
                                                            break;
                                                        }
                                                    }
                                                    if (senderProfilePic) break;

                                                    // Also check for any profile pic image in the sibling
                                                    const imgs = sibling.querySelectorAll('img');
                                                    for (const img of imgs) {
                                                        if (isProfilePic(img)) {
                                                            senderProfilePic = img.src;
                                                            avatarDebug.method = 'prevSiblings-img-' + siblingCount;
                                                            break;
                                                        }
                                                    }
                                                    if (senderProfilePic) break;

                                                    sibling = sibling.previousElementSibling;
                                                    siblingCount++;
                                                }
                                            }

                                            // Check parent container for avatar (broader search)
                                            if (!senderProfilePic && msgRow.parentElement) {
                                                const parentRow = msgRow.parentElement;
                                                const avatars = parentRow.querySelectorAll('img');
                                                for (const avatar of avatars) {
                                                    if (isProfilePic(avatar)) {
                                                        senderProfilePic = avatar.src;
                                                        avatarDebug.method = 'parentRow';
                                                        break;
                                                    }
                                                }
                                            }

                                            // Last resort: check the entire conversation panel for this sender's avatar
                                            if (!senderProfilePic && sender) {
                                                const convPanel = document.querySelector('[data-testid="conversation-panel-messages"]') ||
                                                                 document.querySelector('#main');
                                                if (convPanel) {
                                                    // Look for author elements matching this sender
                                                    const authorElements = convPanel.querySelectorAll('[data-testid="author"], [data-testid="msg-author-title"]');
                                                    for (const authorEl of authorElements) {
                                                        if (authorEl.innerText && authorEl.innerText.trim() === sender.trim()) {
                                                            // Found matching author, look for nearby avatar
                                                            const nearbyContainer = authorEl.closest('[data-testid="msg-container"]') ||
                                                                                   authorEl.closest('[role="row"]') ||
                                                                                   authorEl.parentElement?.parentElement;
                                                            if (nearbyContainer) {
                                                                const avatar = nearbyContainer.querySelector('img[src*="pps.whatsapp.net"]');
                                                                if (avatar && avatar.src) {
                                                                    senderProfilePic = avatar.src;
                                                                    avatarDebug.method = 'convPanel-author-match';
                                                                    break;
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }

                                        // If text is the same as sender name and we have media, use placeholder
                                        // This catches cases where sender name was picked up as text for image messages
                                        let finalText = text;
                                        if (mediaUrl && text && sender && text.trim() === sender.trim()) {
                                            finalText = `[${mediaType || 'media'}]`;
                                        }

                                        // Extract WhatsApp's unique message ID and sender ID from data-id attribute
                                        // Private chat format: "false_PHONE@c.us_MSGID" - PHONE is the sender
                                        // Group chat format: "false_GROUPID@g.us_MSGID_SENDERID@lid" - GROUPID is the chat, SENDERID@lid is the sender
                                        let msgDataId = null;
                                        let senderWhatsappId = null;
                                        const dataIdAttr = msg.getAttribute('data-id');
                                        if (dataIdAttr) {
                                            const parts = dataIdAttr.split('_');
                                            if (parts.length >= 3) {
                                                // Check if this is a group chat message (contains @g.us)
                                                const isGroupMsg = dataIdAttr.includes('@g.us');

                                                if (isGroupMsg) {
                                                    // For group messages: extract sender's LID (the part with @lid)
                                                    // Format: false_GROUPID@g.us_MSGID_SENDERID@lid
                                                    for (let i = parts.length - 1; i >= 0; i--) {
                                                        if (parts[i].includes('@lid')) {
                                                            // Extract just the numeric part before @lid
                                                            senderWhatsappId = parts[i].replace('@lid', '');
                                                            break;
                                                        }
                                                    }
                                                    // If no @lid found, try to get sender from author element later
                                                    // The unique message ID for groups includes everything after the group ID
                                                    msgDataId = parts.slice(2).join('_');
                                                } else {
                                                    // For private chat messages: the sender IS the phone@c.us or ID@lid
                                                    for (let i = 1; i < parts.length; i++) {
                                                        if (parts[i].includes('@c.us')) {
                                                            // Extract phone number without @c.us
                                                            senderWhatsappId = parts[i].replace('@c.us', '');
                                                            break;
                                                        } else if (parts[i].includes('@lid')) {
                                                            // Extract linked ID without @lid (WhatsApp 2025+ format)
                                                            senderWhatsappId = parts[i].replace('@lid', '');
                                                            break;
                                                        }
                                                    }
                                                    // The unique message ID is after the phone/lid
                                                    msgDataId = parts.slice(2).join('_');
                                                }
                                            } else {
                                                msgDataId = dataIdAttr;
                                            }
                                        }

                                        result.push({
                                            text: finalText.substring(0, 500),
                                            isOutgoing: isOutgoing,
                                            sender: sender,
                                            senderProfilePic: senderProfilePic,
                                            avatarDebug: avatarDebug,
                                            senderWhatsappId: senderWhatsappId,
                                            mediaUrl: mediaUrl,
                                            mediaType: mediaType,
                                            mediaFilename: mediaFilename,
                                            msgDataId: msgDataId,
                                            whatsappTimestamp: whatsappTimestamp
                                        });
                                    });

                                    return result;
                                }''')

                                logger.info(f"Bot {bot_profile_id}: Found {len(messages_data)} messages in chat")

                                # Scroll to bottom to mark messages as read
                                page.evaluate('''() => {
                                    const msgContainer = document.querySelector('[data-testid="conversation-panel-messages"]') ||
                                                        document.querySelector('[role="application"]') ||
                                                        document.querySelector('#main');
                                    if (msgContainer) {
                                        msgContainer.scrollTop = msgContainer.scrollHeight;
                                    }
                                }''')

                                # Update cooldown timestamp for this chat
                                # IMPORTANT: Set cooldown for BOTH chat_name AND chat_data_id
                                # because cooldown check uses chat_name (data_id unknown before opening)
                                cooldown_time = time_module.time()
                                processed_chats_cooldown[chat_name] = cooldown_time
                                if chat_data_id and chat_data_id != chat_name:
                                    processed_chats_cooldown[chat_data_id] = cooldown_time

                                # Debug: log message details (only first time or when there's new messages)
                                if len(messages_data) > 0:
                                    logger.info(f"Bot {bot_profile_id}: Messages: {[m.get('text', '')[:20] for m in messages_data]}")

                                incoming_count = 0
                                new_messages = []
                                # Cache sender profile pics by sender ID to reuse across messages
                                sender_pic_cache = {}
                                for idx, msg_data in enumerate(messages_data):
                                    if msg_data['isOutgoing']:
                                        continue

                                    incoming_count += 1
                                    text = msg_data['text']
                                    sender = msg_data['sender']
                                    sender_pic_url = msg_data.get('senderProfilePic')
                                    avatar_debug = msg_data.get('avatarDebug', {})  # Debug info for avatar extraction
                                    sender_whatsapp_id = msg_data.get('senderWhatsappId')  # Sender's WhatsApp ID (e.g., "971524906816@c.us")
                                    media_url = msg_data.get('mediaUrl')
                                    media_type = msg_data.get('mediaType')
                                    media_filename = msg_data.get('mediaFilename')
                                    msg_whatsapp_id = msg_data.get('msgDataId')  # WhatsApp's unique message ID
                                    whatsapp_timestamp = msg_data.get('whatsappTimestamp')  # Timestamp for timezone detection

                                    # DEBUG: Log raw message data from JavaScript
                                    logger.info(f"Bot {bot_profile_id}: RAW MSG idx={idx} text='{text[:30]}' sender={sender} senderId={sender_whatsapp_id} mediaType={media_type} msgId={msg_whatsapp_id or 'None'} ts={whatsapp_timestamp}...")

                                    # Check if this message already exists in database (is it historical?)
                                    # We skip media download for historical messages to avoid mix-ups
                                    # Only download media for NEW messages (the one that triggered unread indicator)
                                    is_historical_message = False
                                    unique_chat_id_check = chat_data_id or chat_name

                                    # CRITICAL FIX: Use WhatsApp's unique message ID for deduplication
                                    # This correctly handles messages with identical text content
                                    if msg_whatsapp_id:
                                        # Use WhatsApp's unique ID - guaranteed unique per message
                                        msg_id_check = f"{unique_chat_id_check}_{msg_whatsapp_id}"
                                    else:
                                        # Fallback to content-based hash (less reliable but works if no ID)
                                        sender_id_check = sender or "unknown"
                                        text_sig_check = f"{text[:10]}_{text[-10:] if len(text) > 10 else ''}"
                                        media_sig_check = media_filename or media_type or ''
                                        msg_id_check = f"{unique_chat_id_check}_{sender_id_check}_{hash(text)}_{len(text)}_{hash(text_sig_check)}_{hash(media_sig_check)}"

                                    # Check if we've already processed this message in this session
                                    if msg_id_check in processed_messages:
                                        is_historical_message = True
                                        logger.info(f"Bot {bot_profile_id}: Message idx={idx} already processed in session, marking as historical")
                                    else:
                                        # Add to processed_messages NOW to prevent duplicate processing in same batch
                                        processed_messages.add(msg_id_check)

                                        # CRITICAL FIX: If we have a WhatsApp message ID, trust it as unique
                                        # Only do database content check when we don't have a reliable unique ID
                                        # This fixes the bug where multiple [image] messages get wrongly matched
                                        should_check_db = not msg_whatsapp_id

                                        # Also check database for existing message with same content (only if no unique ID)
                                        if should_check_db:
                                            try:
                                                from app.database import get_db_session, Message, Conversation
                                                with get_db_session() as check_db:
                                                    existing_conv = check_db.query(Conversation).filter(
                                                        Conversation.bot_profile_id == bot_profile_id,
                                                        Conversation.chat_id == unique_chat_id_check
                                                    ).first()
                                                    if not existing_conv:
                                                        existing_conv = check_db.query(Conversation).filter(
                                                            Conversation.bot_profile_id == bot_profile_id,
                                                            Conversation.chat_name == chat_name
                                                        ).first()

                                                    if existing_conv:
                                                        # Check if a message with same text AND filename exists
                                                        # CRITICAL: For media messages like [document] or [image],
                                                        # we must also check file_name to differentiate between different documents
                                                        if media_filename and text in ['[document]', '[image]', '[video]', '[audio]', '[media]']:
                                                            # For media messages, check both content and filename
                                                            existing_msg = check_db.query(Message).filter(
                                                                Message.conversation_id == existing_conv.id,
                                                                Message.content == text[:2000],
                                                                Message.file_name == media_filename,
                                                                Message.role == 'user'
                                                            ).first()
                                                        else:
                                                            # For regular text messages, just check content
                                                            existing_msg = check_db.query(Message).filter(
                                                                Message.conversation_id == existing_conv.id,
                                                                Message.content == text[:2000],
                                                                Message.role == 'user'
                                                            ).first()
                                                        if existing_msg:
                                                            is_historical_message = True
                                                            logger.info(f"Bot {bot_profile_id}: Message idx={idx} exists in database, marking as historical")
                                            except Exception as check_err:
                                                logger.debug(f"Bot {bot_profile_id}: Error checking message existence: {check_err}")

                                    # Log sender profile pic extraction for debugging
                                    if is_group:
                                        logger.info(f"Bot {bot_profile_id}: Group msg from '{sender}' - sender_pic_url: {bool(sender_pic_url)}, url: {sender_pic_url[:50] if sender_pic_url else 'None'}")
                                        # Log avatar debug details to help diagnose extraction failures
                                        if avatar_debug:
                                            logger.info(f"Bot {bot_profile_id}: Avatar debug - msgRowFound: {avatar_debug.get('msgRowFound')}, allImgCount: {avatar_debug.get('allImgCount')}, method: {avatar_debug.get('method')}")
                                            if avatar_debug.get('allImgSrcs'):
                                                for img_info in avatar_debug.get('allImgSrcs', [])[:3]:  # Log first 3 images
                                                    logger.info(f"Bot {bot_profile_id}: Avatar debug img - src: {img_info.get('src', '')[:80]}, w: {img_info.get('w')}, h: {img_info.get('h')}")

                                    # Use sender's WhatsApp ID or name as cache key
                                    sender_cache_key = sender_whatsapp_id or sender or 'unknown'

                                    # Convert sender profile pic URL to base64 if available
                                    sender_pic = None
                                    if sender_pic_url and not sender_pic_url.startswith('data:'):
                                        try:
                                            sender_pic = page.evaluate('''async (imgUrl) => {
                                                try {
                                                    const response = await fetch(imgUrl);
                                                    const blob = await response.blob();
                                                    return new Promise((resolve, reject) => {
                                                        const reader = new FileReader();
                                                        reader.onloadend = () => resolve(reader.result);
                                                        reader.onerror = reject;
                                                        reader.readAsDataURL(blob);
                                                    });
                                                } catch(e) {
                                                    return null;
                                                }
                                            }''', sender_pic_url)
                                            # Cache the successfully converted profile pic
                                            if sender_pic:
                                                sender_pic_cache[sender_cache_key] = sender_pic
                                                logger.debug(f"Bot {bot_profile_id}: Cached profile pic for sender {sender_cache_key}")
                                        except:
                                            sender_pic = sender_pic_url  # Fallback to URL
                                    elif sender_pic_url and sender_pic_url.startswith('data:'):
                                        # Already base64 encoded
                                        sender_pic = sender_pic_url
                                        sender_pic_cache[sender_cache_key] = sender_pic

                                    # If we didn't get a profile pic, check the cache
                                    if not sender_pic and sender_cache_key in sender_pic_cache:
                                        sender_pic = sender_pic_cache[sender_cache_key]
                                        logger.debug(f"Bot {bot_profile_id}: Using cached profile pic for sender {sender_cache_key}")

                                    # Download and save media if present
                                    # IMPORTANT: Skip download for historical messages - only store media type/filename
                                    file_info = None
                                    if media_url:
                                        logger.info(f"Bot {bot_profile_id}: Found media in message - type: {media_type}, url: {media_url[:50] if len(media_url) > 50 else media_url}..., is_historical={is_historical_message}")

                                        # For HISTORICAL messages: only store media type and filename, skip download
                                        if is_historical_message:
                                            logger.info(f"Bot {bot_profile_id}: HISTORICAL message - skipping media download, storing metadata only")
                                            # Determine MIME type from URL or filename
                                            detected_mime = 'application/octet-stream'
                                            if media_url.startswith('document-click-to-download:'):
                                                detected_mime = media_url.split(':', 1)[1] if ':' in media_url else 'application/octet-stream'
                                            elif media_url.startswith('image-click-to-download:'):
                                                detected_mime = media_url.split(':', 1)[1] if ':' in media_url else 'image/jpeg'
                                            elif media_type == 'image':
                                                detected_mime = 'image/jpeg'
                                            elif media_type == 'video':
                                                detected_mime = 'video/mp4'
                                            elif media_type == 'audio':
                                                detected_mime = 'audio/mpeg'
                                            elif media_type == 'document' and media_filename:
                                                ext = media_filename.split('.')[-1].lower() if '.' in media_filename else ''
                                                mime_map = {'pdf': 'application/pdf', 'doc': 'application/msword', 'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'xls': 'application/vnd.ms-excel', 'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
                                                detected_mime = mime_map.get(ext, 'application/octet-stream')

                                            file_info = {
                                                'file_url': None,  # Not downloaded
                                                'file_type': detected_mime,
                                                'file_name': media_filename or media_type or 'media',
                                                'file_size': 0,
                                                'is_placeholder': True,
                                                'is_historical': True
                                            }
                                            # Don't proceed with download for historical messages

                                        # Only attempt download if file_info not already set (skips historical)
                                        if file_info is None:
                                            try:
                                                # Handle document that needs click to download
                                                if media_url.startswith('document-click-to-download:'):
                                                    detected_mime = media_url.split(':', 1)[1] if ':' in media_url else 'application/octet-stream'
                                                    logger.info(f"Bot {bot_profile_id}: Document detected: {media_filename}, MIME: {detected_mime}")

                                                    # Check if this document already exists in database with file_url
                                                    # Skip download if already downloaded and analyzed
                                                    from app.database import Message, Conversation
                                                    existing_doc = db.query(Message).join(Conversation).filter(
                                                        Conversation.bot_profile_id == bot_profile_id,
                                                        Message.file_name == media_filename,
                                                        Message.file_url.isnot(None)
                                                    ).first()

                                                    if existing_doc and existing_doc.file_url:
                                                        logger.info(f"Bot {bot_profile_id}: Document '{media_filename}' already downloaded, skipping (file_url: {existing_doc.file_url[:50]}...)")
                                                        # Use existing file info
                                                        file_info = {
                                                            'file_url': existing_doc.file_url,
                                                            'file_type': existing_doc.file_type or detected_mime,
                                                            'file_name': existing_doc.file_name,
                                                            'file_size': existing_doc.file_size or 0,
                                                        }
                                                    else:
                                                        # Try to download document using Playwright's native locators
                                                        try:
                                                            logger.info(f"Bot {bot_profile_id}: Attempting to download document '{media_filename}' (msgId: {msg_whatsapp_id})...")
                                                            import tempfile
                                                            import base64 as base64_module

                                                            # STEP 1: Clear Chrome downloads before downloading
                                                            # This ensures the first item in downloads will be our new download
                                                            try:
                                                                logger.info(f"Bot {bot_profile_id}: Clearing Chrome downloads...")
                                                                context = page.context
                                                                clear_page = context.new_page()
                                                                clear_page.goto('chrome://downloads/', timeout=10000)
                                                                time.sleep(0.5)
                                                                clear_page.evaluate('''() => {
                                                                    const manager = document.querySelector('downloads-manager');
                                                                    if (manager && manager.shadowRoot) {
                                                                        const toolbar = manager.shadowRoot.querySelector('downloads-toolbar');
                                                                        if (toolbar && toolbar.shadowRoot) {
                                                                            const clearBtn = toolbar.shadowRoot.querySelector('#clearAll');
                                                                            if (clearBtn) clearBtn.click();
                                                                        }
                                                                    }
                                                                }''')
                                                                time.sleep(0.3)
                                                                clear_page.close()
                                                                logger.info(f"Bot {bot_profile_id}: Downloads cleared")
                                                            except Exception as clear_err:
                                                                logger.debug(f"Bot {bot_profile_id}: Could not clear downloads: {clear_err}")

                                                            doc_found = False

                                                            # STEP 2: Find and click document using message ID for precise targeting
                                                            # This ensures we click the exact document in the correct message
                                                            if msg_whatsapp_id:
                                                                try:
                                                                    logger.info(f"Bot {bot_profile_id}: Trying to find message with data-id containing '{msg_whatsapp_id}'")

                                                                    # Debug: Check what data-id elements exist
                                                                    data_ids = page.evaluate(f'''(msgId) => {{
                                                                        const elements = document.querySelectorAll('[data-id]');
                                                                        const matches = [];
                                                                        for (const el of elements) {{
                                                                            const dataId = el.getAttribute('data-id');
                                                                            if (dataId && dataId.includes(msgId)) {{
                                                                                matches.push({{
                                                                                    dataId: dataId,
                                                                                    tag: el.tagName,
                                                                                    hasDocThumb: !!el.querySelector('[data-testid="document-thumb"]')
                                                                                }});
                                                                            }}
                                                                        }}
                                                                        return matches;
                                                                    }}''', msg_whatsapp_id)
                                                                    logger.info(f"Bot {bot_profile_id}: Found {len(data_ids)} elements with matching data-id: {data_ids}")

                                                                    # Find message container by WhatsApp message ID
                                                                    msg_container = page.locator(f'[data-id*="{msg_whatsapp_id}"]').first
                                                                    if msg_container.is_visible(timeout=2000):
                                                                        logger.info(f"Bot {bot_profile_id}: Message container found and visible")
                                                                        # Find document element within this specific message
                                                                        doc_in_msg = msg_container.locator('[data-testid="document-thumb"]').first
                                                                        if doc_in_msg.is_visible(timeout=1000):
                                                                            doc_in_msg.click(timeout=3000)
                                                                            logger.info(f"Bot {bot_profile_id}: Clicked document in message {msg_whatsapp_id}")
                                                                            doc_found = True
                                                                        else:
                                                                            logger.info(f"Bot {bot_profile_id}: Document thumb not visible in message container")
                                                                    else:
                                                                        logger.info(f"Bot {bot_profile_id}: Message container not visible")
                                                                except Exception as msg_loc_err:
                                                                    logger.info(f"Bot {bot_profile_id}: Could not find document by message ID: {msg_loc_err}")

                                                            # Fallback: Find document by filename if message ID approach failed
                                                            if not doc_found:
                                                                # Escape quotes in filename for CSS selector
                                                                safe_filename = media_filename.replace('"', '\\"').replace("'", "\\'") if media_filename else None
                                                                doc_locators = [
                                                                    page.locator(f'[data-testid="document-thumb"]:has(span[title="{safe_filename}"])').first if safe_filename else None,
                                                                    page.locator(f'span[title="{safe_filename}"]').first if safe_filename else None,
                                                                    page.locator(f'[title="{safe_filename}"]').first if safe_filename else None,
                                                                ]
                                                                doc_locators = [loc for loc in doc_locators if loc is not None]

                                                                for i, doc_locator in enumerate(doc_locators):
                                                                    try:
                                                                        if doc_locator.is_visible(timeout=1000):
                                                                            logger.info(f"Bot {bot_profile_id}: Found document with fallback locator {i+1}")
                                                                            doc_locator.click(timeout=3000)
                                                                            logger.info(f"Bot {bot_profile_id}: Clicked document element")
                                                                            doc_found = True
                                                                            break
                                                                    except Exception as loc_err:
                                                                        logger.debug(f"Bot {bot_profile_id}: Locator {i+1} failed: {loc_err}")
                                                                        continue

                                                            # Fallback: Use JavaScript to find and click with more debugging
                                                            if not doc_found:
                                                                logger.info(f"Bot {bot_profile_id}: Playwright locators failed, trying JavaScript fallback...")

                                                                # Find clickable document elements - NOT parent containers
                                                                doc_element_info = page.evaluate('''(filename) => {
                                                                    // Strategy 1: Find document-thumb elements first (most reliable)
                                                                    const docThumbs = document.querySelectorAll('[data-testid="document-thumb"], [data-testid="pdf-thumb"], [data-testid="document"]');
                                                                    for (const thumb of docThumbs) {
                                                                        const rect = thumb.getBoundingClientRect();
                                                                        if (rect.width > 30 && rect.height > 30 && rect.x > 0 && rect.y > 0) {
                                                                            return {
                                                                                method: 'document-thumb',
                                                                                x: Math.round(rect.x + rect.width / 2),
                                                                                y: Math.round(rect.y + rect.height / 2),
                                                                                w: Math.round(rect.width),
                                                                                h: Math.round(rect.height)
                                                                            };
                                                                        }
                                                                    }

                                                                    // Strategy 2: Find elements with the filename in title (specific span/div)
                                                                    const allElements = document.querySelectorAll('span[title], div[title], [role="button"]');
                                                                    for (const el of allElements) {
                                                                        const title = el.getAttribute('title') || '';
                                                                        if (title.includes(filename) || (filename && el.textContent?.includes(filename))) {
                                                                            const rect = el.getBoundingClientRect();
                                                                            // Only accept reasonably sized elements (not full page containers)
                                                                            if (rect.width > 30 && rect.width < 500 && rect.height > 20 && rect.height < 200) {
                                                                                return {
                                                                                    method: 'title-match',
                                                                                    x: Math.round(rect.x + rect.width / 2),
                                                                                    y: Math.round(rect.y + rect.height / 2),
                                                                                    w: Math.round(rect.width),
                                                                                    h: Math.round(rect.height),
                                                                                    title: title.substring(0, 50)
                                                                                };
                                                                            }
                                                                        }
                                                                    }

                                                                    // Strategy 3: Find the last incoming message with a document
                                                                    const incomingMsgs = document.querySelectorAll('.message-in');
                                                                    for (let i = incomingMsgs.length - 1; i >= 0; i--) {
                                                                        const msg = incomingMsgs[i];
                                                                        const docThumb = msg.querySelector('[data-testid="document-thumb"], [data-testid="pdf-thumb"], [role="button"]');
                                                                        if (docThumb) {
                                                                            const rect = docThumb.getBoundingClientRect();
                                                                            if (rect.width > 30 && rect.height > 30) {
                                                                                return {
                                                                                    method: 'message-in-doc',
                                                                                    x: Math.round(rect.x + rect.width / 2),
                                                                                    y: Math.round(rect.y + rect.height / 2),
                                                                                    w: Math.round(rect.width),
                                                                                    h: Math.round(rect.height)
                                                                                };
                                                                            }
                                                                        }
                                                                    }

                                                                    // Strategy 4: Find any clickable element in the main panel that might be a document
                                                                    const mainPanel = document.querySelector('#main');
                                                                    if (mainPanel) {
                                                                        const buttons = mainPanel.querySelectorAll('[role="button"], button, [data-testid*="doc"], [data-testid*="pdf"]');
                                                                        for (const btn of buttons) {
                                                                            const rect = btn.getBoundingClientRect();
                                                                            if (rect.width > 50 && rect.width < 400 && rect.height > 40 && rect.height < 150) {
                                                                                return {
                                                                                    method: 'main-panel-button',
                                                                                    x: Math.round(rect.x + rect.width / 2),
                                                                                    y: Math.round(rect.y + rect.height / 2),
                                                                                    w: Math.round(rect.width),
                                                                                    h: Math.round(rect.height)
                                                                                };
                                                                            }
                                                                        }
                                                                    }

                                                                    return null;
                                                                }''', media_filename or '')

                                                                if doc_element_info:
                                                                    logger.info(f"Bot {bot_profile_id}: Found document element via {doc_element_info.get('method')} at ({doc_element_info['x']}, {doc_element_info['y']}) size {doc_element_info['w']}x{doc_element_info['h']}")
                                                                    page.mouse.click(doc_element_info['x'], doc_element_info['y'])
                                                                    doc_found = True
                                                                else:
                                                                    logger.warning(f"Bot {bot_profile_id}: Could not find clickable document element for '{media_filename}'")

                                                            if doc_found:
                                                                logger.info(f"Bot {bot_profile_id}: Waiting for WhatsApp to download document (may take time for large files)...")
                                                                time.sleep(30)  # Wait 30 seconds for document to fully download (handles large files and slow networks)

                                                                # Step 2: Open browser downloads page
                                                                # Instead of Ctrl+J (unreliable), open new tab and navigate directly
                                                                logger.info(f"Bot {bot_profile_id}: Opening browser downloads page...")

                                                                context = page.context
                                                                downloads_page = None

                                                                try:
                                                                    # Create new tab and navigate to chrome://downloads
                                                                    downloads_page = context.new_page()
                                                                    downloads_page.goto('chrome://downloads/', timeout=10000)
                                                                    time.sleep(1)  # Wait for page to load
                                                                    logger.info(f"Bot {bot_profile_id}: Opened downloads page: {downloads_page.url}")
                                                                except Exception as nav_err:
                                                                    logger.warning(f"Bot {bot_profile_id}: Failed to open downloads page: {nav_err}")
                                                                    downloads_page = None

                                                                # Find the copy link button for the most recent download
                                                                doc_base64 = None

                                                                if downloads_page:
                                                                    logger.info(f"Bot {bot_profile_id}: Looking for download items on downloads page...")

                                                                    # The downloads page has shadow DOM, need to access it
                                                                    # Get the first (most recent) download item
                                                                    download_info = downloads_page.evaluate('''async () => {
                                                                    const result = {
                                                                        url: window.location.href,
                                                                        hasManager: false,
                                                                        hasItems: false,
                                                                        itemCount: 0,
                                                                        firstItemUrl: null,
                                                                        firstItemName: null,
                                                                        error: null
                                                                    };

                                                                    try {
                                                                        if (!window.location.href.includes('downloads')) {
                                                                            result.error = 'Not on downloads page';
                                                                            return result;
                                                                        }

                                                                        const downloadsManager = document.querySelector('downloads-manager');
                                                                        if (!downloadsManager) {
                                                                            result.error = 'Downloads manager not found';
                                                                            return result;
                                                                        }
                                                                        result.hasManager = true;

                                                                        const shadowRoot = downloadsManager.shadowRoot;
                                                                        if (!shadowRoot) {
                                                                            result.error = 'Shadow root not found';
                                                                            return result;
                                                                        }

                                                                        const downloadItems = shadowRoot.querySelectorAll('downloads-item');
                                                                        result.itemCount = downloadItems.length;

                                                                        if (downloadItems.length === 0) {
                                                                            result.error = 'No download items found';
                                                                            return result;
                                                                        }
                                                                        result.hasItems = true;

                                                                        // Get the first (most recent) download item
                                                                        const downloadItem = downloadItems[0];
                                                                        const itemShadow = downloadItem.shadowRoot;
                                                                        if (!itemShadow) {
                                                                            result.error = 'Item shadow not found';
                                                                            return result;
                                                                        }

                                                                        // Get filename
                                                                        const nameEl = itemShadow.querySelector('#name') ||
                                                                                      itemShadow.querySelector('#file-link') ||
                                                                                      itemShadow.querySelector('a[download]');
                                                                        result.firstItemName = nameEl ? (nameEl.textContent || nameEl.getAttribute('download') || '') : '';

                                                                        // Get URL
                                                                        const urlEl = itemShadow.querySelector('#url') ||
                                                                                     itemShadow.querySelector('#file-link') ||
                                                                                     itemShadow.querySelector('a[href]');
                                                                        result.firstItemUrl = urlEl ? (urlEl.href || urlEl.textContent) : null;

                                                                    } catch(e) {
                                                                        result.error = e.message;
                                                                    }

                                                                    return result;
                                                                }''')

                                                                logger.info(f"Bot {bot_profile_id}: Download page info: {download_info}")

                                                                # Use the first (most recent) download item
                                                                # Note: Chrome generates random filenames for downloads, so we can't match by name
                                                                # We rely on the wait time after clicking to ensure the download completes
                                                                blob_url = download_info.get('firstItemUrl') if download_info else None
                                                                download_name = download_info.get('firstItemName') if download_info else None
                                                                logger.info(f"Bot {bot_profile_id}: Using first download item '{download_name}' for document '{media_filename}'")

                                                                # Close the downloads tab
                                                                try:
                                                                    downloads_page.close()
                                                                    logger.info(f"Bot {bot_profile_id}: Closed downloads tab")
                                                                except:
                                                                    pass

                                                                if blob_url and blob_url.startswith('blob:'):
                                                                    logger.info(f"Bot {bot_profile_id}: Got blob URL: {blob_url[:60]}...")
                                                                    # Fetch blob from WhatsApp page (blob URLs are origin-scoped)
                                                                    logger.info(f"Bot {bot_profile_id}: Fetching blob from WhatsApp page...")
                                                                    doc_base64 = page.evaluate('''async (blobUrl) => {
                                                                        try {
                                                                            const response = await fetch(blobUrl);
                                                                            if (response.ok) {
                                                                                const blob = await response.blob();
                                                                                if (blob.size > 0) {
                                                                                    return new Promise((resolve) => {
                                                                                        const reader = new FileReader();
                                                                                        reader.onloadend = () => resolve(reader.result);
                                                                                        reader.readAsDataURL(blob);
                                                                                    });
                                                                                }
                                                                            }
                                                                        } catch(e) {
                                                                            console.log('Blob fetch error:', e.message);
                                                                        }
                                                                        return null;
                                                                    }''', blob_url)
                                                                    if doc_base64:
                                                                        logger.info(f"Bot {bot_profile_id}: Successfully fetched blob content")
                                                                    else:
                                                                        logger.info(f"Bot {bot_profile_id}: Failed to fetch blob content")
                                                                else:
                                                                    logger.info(f"Bot {bot_profile_id}: No blob URL found in downloads")
                                                            else:
                                                                logger.info(f"Bot {bot_profile_id}: Could not open downloads page")

                                                            if doc_base64:
                                                                logger.info(f"Bot {bot_profile_id}: Got document content from blob")
                                                                # Fix the MIME type in base64 header - Chrome blob may have wrong type
                                                                # Use the detected_mime from WhatsApp instead
                                                                if ',' in doc_base64 and detected_mime:
                                                                    # Replace the MIME type in the data URL header
                                                                    _, base64_content = doc_base64.split(',', 1)
                                                                    doc_base64 = f"data:{detected_mime};base64,{base64_content}"
                                                                    logger.info(f"Bot {bot_profile_id}: Fixed MIME type to: {detected_mime}")
                                                                file_info = _save_incoming_media(doc_base64, 'document', bot_profile_id, chat_name, media_filename)
                                                                if file_info:
                                                                    # Override file_type with correct MIME
                                                                    file_info['file_type'] = detected_mime
                                                                    logger.info(f"Bot {bot_profile_id}: Saved document: {file_info.get('file_url')}")
                                                                else:
                                                                    logger.info(f"Bot {bot_profile_id}: Could not get document content")
                                                            else:
                                                                logger.info(f"Bot {bot_profile_id}: Could not find document element for '{media_filename}'")

                                                        except Exception as doc_err:
                                                            logger.warning(f"Bot {bot_profile_id}: Error during document download: {doc_err}")

                                                    # If document download failed, store metadata as fallback (inside document block)
                                                    if not file_info:
                                                        logger.info(f"Bot {bot_profile_id}: Storing document metadata only (download not available)")
                                                        file_info = {
                                                            'file_url': None,
                                                            'file_type': detected_mime,
                                                            'file_name': media_filename or 'document',
                                                            'file_size': 0,
                                                            'is_placeholder': True
                                                        }

                                                # Handle image that needs click to download
                                                elif media_url.startswith('image-click-to-download:'):
                                                    detected_mime = media_url.split(':', 1)[1] if ':' in media_url else 'image/jpeg'
                                                    logger.info(f"Bot {bot_profile_id}: Image detected that needs clicking to download, MIME: {detected_mime}")

                                                    try:
                                                        # Find and click the image to open full view
                                                        img_found = False

                                                        # Try to find image container using Playwright locators
                                                        img_locators = [
                                                            page.locator('[data-testid="image-thumb"]').last,
                                                            page.locator('[data-testid="media-url-provider"]').last,
                                                            page.locator('[data-testid="media-state-photo"]').last,
                                                            page.locator('div[role="button"] img').last,
                                                        ]

                                                        for i, img_locator in enumerate(img_locators):
                                                            try:
                                                                if img_locator.is_visible(timeout=1000):
                                                                    logger.info(f"Bot {bot_profile_id}: Found image with locator strategy {i+1}")
                                                                    img_locator.click(timeout=3000)
                                                                    img_found = True
                                                                    logger.info(f"Bot {bot_profile_id}: Clicked image element to open full view")
                                                                    break
                                                            except Exception as loc_err:
                                                                logger.debug(f"Bot {bot_profile_id}: Image locator {i+1} failed: {loc_err}")
                                                                continue

                                                        if img_found:
                                                            # Wait for the full-size image to load in the overlay
                                                            time.sleep(1.5)

                                                            # Get the blob URL from the full-size image in the overlay
                                                            img_base64 = page.evaluate('''async () => {
                                                            try {
                                                                // Look for the full-size image in the overlay/modal
                                                                const fullImg = document.querySelector('[data-testid="media-viewer"] img') ||
                                                                               document.querySelector('[data-testid="image-viewer"] img') ||
                                                                               document.querySelector('div[role="dialog"] img') ||
                                                                               document.querySelector('div[class*="overlay"] img[src*="blob:"]') ||
                                                                               document.querySelector('img[src*="blob:"][class*="full"]') ||
                                                                               document.querySelector('img[src*="blob:"]:not([data-testid="image-thumb"] img)');

                                                                if (fullImg && fullImg.src && fullImg.src.startsWith('blob:')) {
                                                                    console.log('Found full-size image:', fullImg.src.substring(0, 50));
                                                                    const response = await fetch(fullImg.src);
                                                                    if (!response.ok) return null;
                                                                    const blob = await response.blob();
                                                                    return new Promise((resolve) => {
                                                                        const reader = new FileReader();
                                                                        reader.onloadend = () => resolve(reader.result);
                                                                        reader.onerror = () => resolve(null);
                                                                        reader.readAsDataURL(blob);
                                                                    });
                                                                }

                                                                // Fallback: try to find any large blob image
                                                                const allImgs = document.querySelectorAll('img[src*="blob:"]');
                                                                for (const img of allImgs) {
                                                                    if (img.naturalWidth > 200 && img.naturalHeight > 200) {
                                                                        console.log('Found large blob image:', img.src.substring(0, 50));
                                                                        const response = await fetch(img.src);
                                                                        if (!response.ok) continue;
                                                                        const blob = await response.blob();
                                                                        return new Promise((resolve) => {
                                                                            const reader = new FileReader();
                                                                            reader.onloadend = () => resolve(reader.result);
                                                                            reader.onerror = () => resolve(null);
                                                                            reader.readAsDataURL(blob);
                                                                        });
                                                                    }
                                                                }

                                                                return null;
                                                            } catch(e) {
                                                                console.error('Error getting full image:', e);
                                                                return null;
                                                            }
                                                            }''')

                                                            # Close the image viewer by pressing Escape
                                                            try:
                                                                page.keyboard.press('Escape')
                                                                time.sleep(0.3)
                                                            except:
                                                                pass

                                                            if img_base64:
                                                                logger.info(f"Bot {bot_profile_id}: Got full-size image content")
                                                                file_info = _save_incoming_media(img_base64, 'image', bot_profile_id, chat_name, media_filename)
                                                                if file_info:
                                                                    logger.info(f"Bot {bot_profile_id}: Saved image: {file_info.get('file_url')}")
                                                            else:
                                                                logger.info(f"Bot {bot_profile_id}: Could not get full-size image content")
                                                        else:
                                                            logger.info(f"Bot {bot_profile_id}: Could not find image element to click")

                                                    except Exception as img_err:
                                                        logger.warning(f"Bot {bot_profile_id}: Error during image download: {img_err}")

                                                    # If download failed, store placeholder
                                                    if not file_info:
                                                        logger.info(f"Bot {bot_profile_id}: Storing image metadata only (download not available)")
                                                        file_info = {
                                                            'file_url': None,
                                                            'file_type': detected_mime,
                                                            'file_name': media_filename or 'image',
                                                            'file_size': 0,
                                                            'is_placeholder': True
                                                        }

                                                # Handle special markers for non-downloadable content
                                                # Skip if already handled above (file_info will be set)
                                                elif file_info is None and (media_url == 'document-no-download' or media_url.startswith('document-no-download:')):
                                                    # Document detected but can't download - store metadata only
                                                    # Extract MIME type if provided (format: document-no-download:mime/type)
                                                    detected_mime = 'application/octet-stream'
                                                    if ':' in media_url:
                                                        detected_mime = media_url.split(':', 1)[1]

                                                    logger.info(f"Bot {bot_profile_id}: Document detected but blob URL not available. Filename: {media_filename}, MIME: {detected_mime}")
                                                    file_info = {
                                                        'file_url': None,
                                                        'file_type': detected_mime,
                                                        'file_name': media_filename or 'document',
                                                        'file_size': 0,
                                                        'is_placeholder': True  # Mark as placeholder
                                                    }
                                                elif media_url == 'lottie-animation':
                                                    # Lottie animated emoji - can't download as static image
                                                    logger.info(f"Bot {bot_profile_id}: Lottie animation detected (can't save as image)")
                                                    file_info = {
                                                        'file_url': None,
                                                        'file_type': 'application/lottie',
                                                        'file_name': 'animated_emoji.json',
                                                        'file_size': 0,
                                                        'is_placeholder': True
                                                    }
                                                elif file_info is None:
                                                    # Download media from blob URL and convert to base64
                                                    # Retry up to 3 times with delay (blob URLs can be flaky)
                                                    media_base64 = None
                                                    for retry in range(3):
                                                        media_base64 = page.evaluate('''async (mediaUrl) => {
                                                            try {
                                                                const response = await fetch(mediaUrl);
                                                                if (!response.ok) return null;
                                                                const blob = await response.blob();
                                                                if (blob.size === 0) return null;
                                                                return new Promise((resolve, reject) => {
                                                                    const reader = new FileReader();
                                                                    reader.onloadend = () => resolve(reader.result);
                                                                    reader.onerror = reject;
                                                                    reader.readAsDataURL(blob);
                                                                });
                                                            } catch(e) {
                                                                console.error('Error downloading media:', e);
                                                                return null;
                                                            }
                                                        }''', media_url)
                                                        if media_base64:
                                                            break
                                                        if retry < 2:
                                                            logger.info(f"Bot {bot_profile_id}: Retry {retry + 1}/3 downloading media...")
                                                            time.sleep(0.5)

                                                    if media_base64:
                                                        # Check for duplicate media using content hash
                                                        content_hash = hashlib.md5(media_base64.encode()).hexdigest()
                                                        if chat_name not in saved_media_hashes:
                                                            saved_media_hashes[chat_name] = {}

                                                        if content_hash in saved_media_hashes[chat_name]:
                                                            # Duplicate - retrieve existing file_info
                                                            file_info = saved_media_hashes[chat_name][content_hash]
                                                            logger.info(f"Bot {bot_profile_id}: Using existing media for {chat_name}: {file_info.get('file_url')}")
                                                        else:
                                                            file_info = _save_incoming_media(media_base64, media_type, bot_profile_id, chat_name, media_filename)
                                                            if file_info:
                                                                saved_media_hashes[chat_name][content_hash] = file_info
                                                            else:
                                                                logger.warning(f"Bot {bot_profile_id}: Failed to save incoming media")
                                                    else:
                                                        logger.warning(f"Bot {bot_profile_id}: Could not download media after 3 retries from {media_url[:50]}")
                                            except Exception as media_err:
                                                logger.error(f"Bot {bot_profile_id}: Error downloading media: {media_err}", exc_info=True)

                                    # Use WhatsApp's unique message ID for deduplication
                                    # This ensures the same message is recognized regardless of which path processes it
                                    unique_chat_id = chat_data_id or chat_name

                                    # CRITICAL: Use msg_whatsapp_id consistently with open chat path
                                    if msg_whatsapp_id:
                                        msg_id = f"{unique_chat_id}_{msg_whatsapp_id}"
                                    else:
                                        # Fallback to content-based hash if no message ID available
                                        sender_id = sender or "unknown"
                                        text_sig = f"{text[:10]}_{text[-10:] if len(text) > 10 else ''}"
                                        if file_info:
                                            media_sig = file_info.get('file_url') or file_info.get('file_name') or ''
                                        else:
                                            media_sig = ''
                                        msg_id = f"{unique_chat_id}_{sender_id}_{hash(text)}_{len(text)}_{hash(text_sig)}_{hash(media_sig)}"

                                    # DEBUG: Log msg_id details for troubleshooting deduplication
                                    logger.info(f"Bot {bot_profile_id}: MSG_ID DEBUG idx={idx} text='{text[:30]}' whatsapp_id={msg_whatsapp_id or 'None'} file_info={bool(file_info)} is_historical={is_historical_message}")

                                    # Deduplication check: use is_historical_message which was set at line 3600-3605
                                    # if msg_id_check was already in processed_messages
                                    if is_historical_message:
                                        logger.info(f"Bot {bot_profile_id}: SKIPPING historical message: {text[:30]}...")
                                        continue

                                    # New message - add to processing queue (includes timestamp and message ID for proper storage)
                                    new_messages.append((text, sender, sender_pic, file_info, sender_whatsapp_id, whatsapp_timestamp, msg_whatsapp_id))

                                # Process ALL new incoming messages (store in DB)
                                # But only generate AI response for the LAST message to avoid spam
                                if new_messages:
                                    # IMPORTANT: Sync history BEFORE storing new messages
                                    # This ensures historical messages have earlier timestamps
                                    from app.database import get_db_session, Conversation
                                    with get_db_session() as db:
                                        conv = db.query(Conversation).filter(
                                            Conversation.bot_profile_id == bot_profile_id,
                                            Conversation.chat_id == chat_data_id
                                        ).first() if chat_data_id else None

                                        if not conv:
                                            conv = db.query(Conversation).filter(
                                                Conversation.bot_profile_id == bot_profile_id,
                                                Conversation.chat_name == chat_name
                                            ).first()

                                        needs_sync = conv and not conv.history_synced

                                    if needs_sync:
                                        logger.info(f"Bot {bot_profile_id}: Pre-syncing history for {chat_name} before storing new messages")
                                        _sync_conversation_history_on_demand(page, bot_profile_id, conv.id, chat_name)

                                    for idx, (text, sender, sender_pic, file_info, sender_wa_id, whatsapp_ts, msg_wa_id) in enumerate(new_messages):
                                        is_last_message = (idx == len(new_messages) - 1)

                                        # Clean placeholder text for media-only messages
                                        # Placeholders like [image], [video] etc. should not be stored as content
                                        message_content = text
                                        if file_info and re.match(r'^\[(image|video|audio|document|sticker|media)\]$', text, re.IGNORECASE):
                                            message_content = ''  # No caption, just media

                                        logger.info(f"Bot {bot_profile_id}: Processing message {idx+1}/{len(new_messages)} from {sender or chat_name} (wa_id: {sender_wa_id}, msg_id: {msg_wa_id}): {text[:50]}... (has_media: {bool(file_info)}, is_last: {is_last_message})")

                                        _process_message_sync(
                                            page=page,
                                            ai_provider=ai_provider,
                                            config=config,
                                            bot_profile_id=bot_profile_id,
                                            chat_name=chat_name,
                                            is_group=is_group_from_id or is_group,
                                            sender=sender,
                                            message=message_content,
                                            contact_profile_pic=contact_profile_pic,
                                            sender_profile_pic=sender_pic,
                                            chat_data_id=chat_data_id,
                                            file_info=file_info,
                                            skip_ai_response=(not is_last_message),  # Only AI response for last msg
                                            sender_whatsapp_id=sender_wa_id,  # Sender's WhatsApp ID for tracking
                                            whatsapp_timestamp=whatsapp_ts,  # For timezone offset detection and actual timestamp
                                            whatsapp_message_id=msg_wa_id  # WhatsApp's unique message ID
                                        )

                                    # After processing all messages, close the chat window
                                    # This ensures we can see unread indicators for other chats
                                    try:
                                        # Press Escape to close the chat/go back to chat list
                                        page.keyboard.press('Escape')
                                        time.sleep(0.3)
                                        logger.info(f"Bot {bot_profile_id}: Closed chat window for '{chat_name}'")
                                    except Exception as close_err:
                                        logger.debug(f"Bot {bot_profile_id}: Could not close chat: {close_err}")
                                else:
                                    # All messages were historical - add LONGER cooldown (60 seconds)
                                    # This prevents the bot from reopening the same chat repeatedly
                                    # when the unread indicator doesn't clear properly
                                    # IMPORTANT: Add cooldown for BOTH chat_name AND chat_data_id
                                    # because cooldown check uses chat_name (data_id unknown before opening)
                                    # but we have data_id after opening
                                    cooldown_time = time_module.time() + 55  # 60 second effective cooldown
                                    processed_chats_cooldown[chat_name] = cooldown_time
                                    if chat_data_id and chat_data_id != chat_name:
                                        processed_chats_cooldown[chat_data_id] = cooldown_time
                                    logger.info(f"Bot {bot_profile_id}: All messages historical in '{chat_name}', adding 60s cooldown (keys: {chat_name}, {chat_data_id})")
                                    try:
                                        page.keyboard.press('Escape')
                                        time.sleep(0.3)
                                        logger.info(f"Bot {bot_profile_id}: Closed chat '{chat_name}' (all messages historical)")
                                    except Exception as close_err:
                                        logger.debug(f"Bot {bot_profile_id}: Could not close chat: {close_err}")

                            except Exception as e:
                                logger.error(f"Bot {bot_profile_id}: Error processing chat row: {e}", exc_info=True)

                    except Exception as e:
                        logger.error(f"Bot {bot_profile_id}: Error in chat processing: {e}", exc_info=True)

                time.sleep(2)

            except Exception as e:
                logger.error(f"Bot {bot_profile_id}: Message loop error: {e}", exc_info=True)
                time.sleep(5)

    except Exception as e:
        import traceback
        logger.error(f"Bot {bot_profile_id}: Error - {e}")
        logger.error(f"Bot {bot_profile_id}: Traceback:\n{traceback.format_exc()}")
        instance.error = str(e)
    finally:
        instance.is_running = False
        instance.whatsapp_connected = False
        instance.browser_connected = False

        # Remove page reference
        with _bot_pages_lock:
            if bot_profile_id in _bot_pages:
                del _bot_pages[bot_profile_id]
                logger.info(f"Bot {bot_profile_id}: Removed page reference")

        # Close context (persistent context - session data is auto-saved)
        if context:
            try:
                context.close()
                logger.info(f"Bot {bot_profile_id}: Browser context closed, session persisted")
            except:
                pass
        if playwright:
            playwright.stop()

        with get_db_session() as db:
            profile = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
            if profile:
                profile.is_running = False
                profile.whatsapp_connected = False

            # Determine appropriate log details
            if instance.stopped_by_user:
                log_details = "Stopped by user"
            elif instance.error:
                log_details = instance.error
            else:
                log_details = "Normal shutdown"

            log = ActivityLog(
                bot_profile_id=bot_profile_id,
                action="bot_stopped",
                details=log_details
            )
            db.add(log)


def _process_message_sync(page, ai_provider, config, bot_profile_id, chat_name, is_group, sender, message, contact_profile_pic=None, sender_profile_pic=None, chat_data_id=None, file_info=None, skip_ai_response=False, sender_whatsapp_id=None, whatsapp_timestamp=None, whatsapp_message_id=None):
    """Synchronous message processing. If skip_ai_response=True, only stores the message without generating AI response.

    Args:
        sender_whatsapp_id: The sender's WhatsApp ID (e.g., "971524906816@c.us") for tracking unique senders.
        whatsapp_timestamp: Raw timestamp from data-pre-plain-text (e.g., "7:13 AM, 1/20/2026") for timezone detection and actual message timestamp.
        whatsapp_message_id: WhatsApp's unique message ID for deduplication and tracking.
    """
    from app.database import get_db_session, Conversation, Message, ActivityLog, BotProfile
    import time
    import re

    # Detect and store timezone offset if we have a timestamp and offset is not yet detected
    if whatsapp_timestamp:
        try:
            with get_db_session() as tz_db:
                bot_profile = tz_db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
                if bot_profile and bot_profile.whatsapp_timezone_offset is None:
                    # Extract time and date from timestamp (format: "HH:MM AM/PM, M/D/YYYY")
                    # Support both English and Arabic AM/PM
                    ts_match = re.match(r'(\d{1,2}:\d{2})\s*([APMapm]{2}|[صم]),?\s*(\d{1,2}/\d{1,2}/\d{4})?', whatsapp_timestamp)
                    if ts_match:
                        time_str = ts_match.group(1) + ' ' + ts_match.group(2)
                        date_str = ts_match.group(3)
                        detected_offset = _detect_whatsapp_timezone_offset(time_str, date_str)
                        if detected_offset is not None:
                            bot_profile.whatsapp_timezone_offset = detected_offset
                            tz_db.commit()
                            logger.info(f"Bot {bot_profile_id}: Stored WhatsApp timezone offset: UTC{detected_offset:+d}")
        except Exception as tz_err:
            logger.debug(f"Bot {bot_profile_id}: Error detecting timezone offset: {tz_err}")

    # Use the FULL WhatsApp data-id as chat_id (e.g., "971524906816@c.us" or "120363...@g.us")
    # This is the unique identifier that never changes for each WhatsApp conversation
    if chat_data_id:
        chat_id = chat_data_id  # Use full data-id directly - guaranteed unique
    else:
        # Fallback: use chat_name if no data-id available (shouldn't happen normally)
        chat_id = chat_name.replace(" ", "_").replace("+", "")

    logger.info(f"Bot {bot_profile_id}: Received from {chat_name} (chat_id: {chat_id}): {message[:50]}...")

    with get_db_session() as db:
        # Find conversation by unique data-id
        conversation = db.query(Conversation).filter(
            Conversation.bot_profile_id == bot_profile_id,
            Conversation.chat_id == chat_id
        ).first()

        # Migration: If we have data-id but no conversation found, check for old format conversations
        if not conversation and chat_data_id:
            # Strategy 1: Look for conversation with name-based chat_id (legacy format)
            name_based_chat_id = chat_name.replace(" ", "_").replace("+", "")
            old_conversation = db.query(Conversation).filter(
                Conversation.bot_profile_id == bot_profile_id,
                Conversation.chat_id == name_based_chat_id
            ).first()

            if old_conversation:
                logger.info(f"Bot {bot_profile_id}: Migrating conversation from name-based ID '{name_based_chat_id}' to data-id '{chat_id}'")
                old_conversation.chat_id = chat_id
                db.commit()
                conversation = old_conversation

            # Strategy 2: Look for conversation with old phone/group format (e.g., "971524906816" or "group_12036...")
            if not conversation:
                # Extract phone/group from data-id for old format lookup
                if '@c.us' in chat_data_id or '@lid' in chat_data_id:
                    old_phone_id = chat_data_id.split('@')[0]
                    old_conversation = db.query(Conversation).filter(
                        Conversation.bot_profile_id == bot_profile_id,
                        Conversation.chat_id == old_phone_id
                    ).first()
                elif '@g.us' in chat_data_id:
                    old_group_id = f"group_{chat_data_id.split('@')[0]}"
                    old_conversation = db.query(Conversation).filter(
                        Conversation.bot_profile_id == bot_profile_id,
                        Conversation.chat_id == old_group_id
                    ).first()

                if old_conversation:
                    logger.info(f"Bot {bot_profile_id}: Migrating conversation from old format '{old_conversation.chat_id}' to data-id '{chat_id}'")
                    old_conversation.chat_id = chat_id
                    db.commit()
                    conversation = old_conversation

            # Strategy 3: Look for any conversation with matching chat_name AND same is_group status
            # IMPORTANT: Only migrate if is_group matches to prevent group/private chat mixups
            if not conversation:
                is_new_group = '@g.us' in chat_data_id
                existing_by_name = db.query(Conversation).filter(
                    Conversation.bot_profile_id == bot_profile_id,
                    Conversation.chat_name == chat_name,
                    Conversation.is_group == is_new_group  # MUST match is_group
                ).first()

                if existing_by_name:
                    logger.info(f"Bot {bot_profile_id}: Found conversation by name '{chat_name}' with matching is_group={is_new_group}, migrating from chat_id '{existing_by_name.chat_id}' to '{chat_id}'")
                    existing_by_name.chat_id = chat_id
                    db.commit()
                    conversation = existing_by_name
                else:
                    # Log if we found a conversation with same name but different is_group
                    wrong_type = db.query(Conversation).filter(
                        Conversation.bot_profile_id == bot_profile_id,
                        Conversation.chat_name == chat_name
                    ).first()
                    if wrong_type:
                        logger.warning(f"Bot {bot_profile_id}: Found conversation with name '{chat_name}' but is_group mismatch (existing: {wrong_type.is_group}, new: {is_new_group}). Will create new conversation.")

        # Fallback: if we DON'T have valid data-id, look for existing conversation with valid WhatsApp ID
        # OPTION A + D: Stricter extraction + Better fallback logic
        if not conversation and not _is_valid_whatsapp_id(chat_data_id):
            logger.warning(f"Bot {bot_profile_id}: NO valid chat_data_id available for '{chat_name}' (is_group={is_group}, got: {chat_data_id}) - searching for existing conversation with valid WhatsApp ID")

            # PRIORITY 1: Find ANY conversation with same name that has a VALID WhatsApp ID
            # This is the safest approach - trust existing valid IDs over creating new
            all_matching = db.query(Conversation).filter(
                Conversation.bot_profile_id == bot_profile_id,
                Conversation.chat_name == chat_name
            ).all()

            # First, try to find one with a valid WhatsApp ID
            for existing in all_matching:
                if _is_valid_whatsapp_id(existing.chat_id):
                    logger.info(f"Bot {bot_profile_id}: Found existing conversation with name '{chat_name}' that has valid WhatsApp ID: {existing.chat_id}. Using this instead of creating duplicate.")
                    conversation = existing
                    # Update is_group based on the valid chat_id
                    if '@g.us' in existing.chat_id:
                        is_group = True
                    elif '@c.us' in existing.chat_id or '@lid' in existing.chat_id:
                        is_group = False
                    break

            # PRIORITY 2: If no valid-ID conversation found, use any existing with matching is_group
            if not conversation:
                for existing in all_matching:
                    if existing.is_group == is_group:
                        logger.info(f"Bot {bot_profile_id}: Found existing conversation by name '{chat_name}' with matching is_group={is_group} (chat_id: {existing.chat_id})")
                        conversation = existing
                        break

            # PRIORITY 3: If still no match, use any existing (to prevent duplicates)
            if not conversation and all_matching:
                existing = all_matching[0]
                logger.warning(f"Bot {bot_profile_id}: Using existing conversation '{chat_name}' with is_group={existing.is_group} (chat_id: {existing.chat_id}) to prevent duplicate creation.")
                conversation = existing
                is_group = existing.is_group

        # Note: Main duplicate check happens later (around line 2864) after conversation is resolved
        # This early check is just for quickly skipping already-responded messages in existing conversations

        if not conversation:
            # OPTION A: NEVER create conversation without valid WhatsApp ID
            # Only create if we have a valid chat_data_id
            if not _is_valid_whatsapp_id(chat_data_id):
                logger.error(f"Bot {bot_profile_id}: REFUSING to create conversation for '{chat_name}' without valid WhatsApp ID. chat_data_id='{chat_data_id}' is invalid. Message will be skipped.")
                return  # Skip this message - don't create conversation with invalid ID

            # Determine display_name
            is_phone_format = bool(re.match(r'^[\+\d\s\-\(\)]+$', chat_name.strip()))
            display_name = chat_name if not is_phone_format and not is_group else None

            # Extract phone from chat_data_id or chat_name
            phone = None
            if chat_data_id and '@c.us' in chat_data_id:
                # @c.us format contains actual phone number
                phone_match = re.search(r'(\d{8,15})@c\.us', chat_data_id)
                if phone_match:
                    phone = phone_match.group(1)
            elif chat_data_id and '@lid' in chat_data_id:
                # @lid format does NOT contain phone - extract from chat_name instead
                # chat_name for private chats often shows the phone (e.g., "+60 16-260 9676")
                if is_phone_format and chat_name:
                    # Clean phone number: remove +, spaces, dashes, parentheses
                    phone = re.sub(r'[\+\s\-\(\)]', '', chat_name.strip())
                    logger.info(f"Bot {bot_profile_id}: Extracted phone '{phone}' from chat_name '{chat_name}' (using @lid format)")

            conversation = Conversation(
                bot_profile_id=bot_profile_id,
                chat_id=chat_id,
                chat_name=chat_name,
                display_name=display_name,
                phone=phone,
                is_group=is_group,
                profile_pic=contact_profile_pic
            )
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
            logger.info(f"Bot {bot_profile_id}: Created new conversation '{chat_name}' (chat_id: {chat_id}, phone: {phone}) with profile_pic: {bool(contact_profile_pic)}")
        else:
            # Update chat_name if the new name is better (a real name vs phone number)
            # IMPORTANT: Also verify is_group matches to prevent accidental cross-type updates
            if chat_name and conversation.chat_name != chat_name:
                # Verify is_group matches before updating name
                is_new_group = is_group or (chat_data_id and '@g.us' in chat_data_id)
                if conversation.is_group != is_new_group:
                    logger.warning(f"Bot {bot_profile_id}: Refusing to update chat_name from '{conversation.chat_name}' to '{chat_name}' - is_group mismatch (conversation: {conversation.is_group}, message: {is_new_group})")
                else:
                    is_new_name_phone = bool(re.match(r'^[\+\d\s\-\(\)]+$', chat_name.strip()))
                    is_current_name_phone = bool(re.match(r'^[\+\d\s\-\(\)]+$', (conversation.chat_name or '').strip()))
                    # Only update if: new is a name, or current is a phone
                    if not is_new_name_phone or is_current_name_phone:
                        logger.info(f"Bot {bot_profile_id}: Updated chat_name from '{conversation.chat_name}' to '{chat_name}'")
                        conversation.chat_name = chat_name

            # Update display_name if chat_name looks like a name (not phone)
            if chat_name and not bool(re.match(r'^[\+\d\s\-\(\)]+$', chat_name.strip())):
                if not conversation.display_name:
                    conversation.display_name = chat_name
                    logger.info(f"Bot {bot_profile_id}: Set display_name to '{chat_name}'")

            # Update profile pic if we have a new one
            if contact_profile_pic and not conversation.profile_pic:
                conversation.profile_pic = contact_profile_pic
                logger.info(f"Bot {bot_profile_id}: Updated conversation profile_pic for {chat_name}")

            # Update phone if we have chat_data_id and conversation doesn't have phone yet
            if chat_data_id and not conversation.phone:
                if '@c.us' in chat_data_id:
                    # @c.us format contains actual phone number
                    phone_match = re.search(r'(\d{8,15})@c\.us', chat_data_id)
                    if phone_match:
                        conversation.phone = phone_match.group(1)
                        logger.info(f"Bot {bot_profile_id}: Updated conversation phone to {conversation.phone}")
                elif '@lid' in chat_data_id:
                    # @lid format does NOT contain phone - extract from chat_name instead
                    is_phone_format = bool(re.match(r'^[\+\d\s\-\(\)]+$', chat_name.strip())) if chat_name else False
                    if is_phone_format and chat_name:
                        phone = re.sub(r'[\+\s\-\(\)]', '', chat_name.strip())
                        conversation.phone = phone
                        logger.info(f"Bot {bot_profile_id}: Updated conversation phone to {phone} (from chat_name, @lid format)")

            # Update is_group if we have data-id
            if chat_data_id:
                if '@g.us' in chat_data_id and not conversation.is_group:
                    conversation.is_group = True
                    logger.info(f"Bot {bot_profile_id}: Updated conversation to is_group=True")
                elif ('@c.us' in chat_data_id or '@lid' in chat_data_id) and conversation.is_group:
                    conversation.is_group = False
                    logger.info(f"Bot {bot_profile_id}: Updated conversation to is_group=False")

            db.commit()

        # CRITICAL: Sync history if NOT yet synced (based on history_synced flag, not is_new_conversation)
        # This handles:
        # 1. Brand new conversation - sync history which includes new message(s)
        # 2. Existing conversation created by scan but history not synced yet
        needs_history_sync = not conversation.history_synced
        skip_message_store = False
        from datetime import datetime, timedelta

        if needs_history_sync:
            logger.info(f"Bot {bot_profile_id}: Syncing history for conversation '{chat_name}' (history_synced=False)")
            _sync_conversation_history_on_demand(page, bot_profile_id, conversation.id, chat_name)
            # Re-fetch conversation after sync
            db.expire(conversation)
            conversation = db.query(Conversation).filter(Conversation.id == conversation.id).first()
            if not conversation:
                logger.error(f"Bot {bot_profile_id}: Failed to re-fetch conversation after sync")
                return

            # After sync, check if this message was included in the sync
            # IMPORTANT: Skip duplicate check for media messages - each image needs its own DB entry
            if not file_info:
                # Text-only message - check for duplicates in synced history
                # PRIMARY: Use WhatsApp message ID if available (unique per message)
                existing_synced_msg = None
                if whatsapp_message_id:
                    existing_synced_msg = db.query(Message).filter(
                        Message.conversation_id == conversation.id,
                        Message.whatsapp_message_id == whatsapp_message_id
                    ).first()
                    if existing_synced_msg:
                        logger.info(f"Bot {bot_profile_id}: Found existing message by WhatsApp ID: {whatsapp_message_id[:20]}...")

                # FALLBACK: Content-based check, but ONLY for recent messages (last 5 minutes)
                # This catches rapid duplicates but allows legitimate repeat messages (e.g., asking same question hours later)
                if not existing_synced_msg:
                    five_mins_ago = datetime.utcnow() - timedelta(minutes=5)
                    existing_synced_msg = db.query(Message).filter(
                        Message.conversation_id == conversation.id,
                        Message.role == 'user',
                        Message.content == message,
                        Message.timestamp > five_mins_ago  # Only check recent messages!
                    ).order_by(Message.timestamp.desc()).first()

                if existing_synced_msg:
                    # Check if there's a response AFTER this message
                    next_response = db.query(Message).filter(
                        Message.conversation_id == conversation.id,
                        Message.role == 'assistant',
                        Message.timestamp > existing_synced_msg.timestamp
                    ).first()

                    if next_response:
                        # Skip entirely to avoid duplicates
                        skip_message_store = True
                        logger.info(f"Bot {bot_profile_id}: Message already in synced history WITH response, skipping entirely: {message[:30]}...")
                        return
                    else:
                        # Message exists but NO response - this is the new incoming message
                        # Don't store again, but DO generate AI response
                        skip_message_store = True
                        logger.info(f"Bot {bot_profile_id}: Message in synced history but NO response, will generate AI response")
            else:
                # Media message - always store as new (each image is unique)
                logger.info(f"Bot {bot_profile_id}: New media message after sync, will store with file_url: {file_info.get('file_url')}")
        else:
            # History already synced - check for duplicates
            # IMPORTANT: Skip duplicate check for media messages - each image needs its own DB entry
            # Content-based duplicate check only applies to text-only messages
            if not file_info:
                # Text-only message - check for duplicates
                # PRIMARY: Use WhatsApp message ID if available (unique per message)
                existing_msg_in_db = None
                if whatsapp_message_id:
                    existing_msg_in_db = db.query(Message).filter(
                        Message.conversation_id == conversation.id,
                        Message.whatsapp_message_id == whatsapp_message_id
                    ).first()
                    if existing_msg_in_db:
                        logger.info(f"Bot {bot_profile_id}: Found existing message by WhatsApp ID: {whatsapp_message_id[:20]}...")

                # FALLBACK: Content-based check, but ONLY for recent messages (last 5 minutes)
                # This catches rapid duplicates but allows legitimate repeat messages (e.g., asking same question hours later)
                if not existing_msg_in_db:
                    five_mins_ago = datetime.utcnow() - timedelta(minutes=5)
                    existing_msg_in_db = db.query(Message).filter(
                        Message.conversation_id == conversation.id,
                        Message.role == 'user',
                        Message.content == message,
                        Message.timestamp > five_mins_ago  # Only check recent messages!
                    ).order_by(Message.timestamp.desc()).first()

                if existing_msg_in_db:
                    # Check if there's a response AFTER this message
                    has_existing_response = db.query(Message).filter(
                        Message.conversation_id == conversation.id,
                        Message.role == 'assistant',
                        Message.timestamp > existing_msg_in_db.timestamp
                    ).first()

                    if has_existing_response:
                        skip_message_store = True
                        logger.info(f"Bot {bot_profile_id}: Message already has response in history, skipping entirely: {message[:30]}...")
                        return
                    else:
                        # Message exists but NO response - this is likely the new incoming message
                        # Don't store again, but DO generate AI response
                        skip_message_store = True
                        logger.info(f"Bot {bot_profile_id}: Message in DB but no response, will generate AI response")
            else:
                # Media message - always store as new (each image is unique)
                logger.info(f"Bot {bot_profile_id}: New media message, will store with file_url: {file_info.get('file_url')}")

        # For profile pics:
        # - In groups: Use the extracted sender_profile_pic, or look up from sender's private conversation
        # - In private chats: Use the conversation's contact profile pic (since sender isn't shown per-message)
        effective_sender_pic = sender_profile_pic

        if not effective_sender_pic and is_group and sender:
            # For groups: Try to find sender's profile pic from their private conversation
            sender_private_conv = db.query(Conversation).filter(
                Conversation.bot_profile_id == bot_profile_id,
                Conversation.is_group == False,
                Conversation.chat_name == sender
            ).first()

            if sender_private_conv and sender_private_conv.profile_pic:
                effective_sender_pic = sender_private_conv.profile_pic
                logger.info(f"Bot {bot_profile_id}: Using profile pic from {sender}'s private conversation for group message")

        # Additional fallback for groups: Look up sender's profile pic from previous messages in this conversation
        if not effective_sender_pic and is_group and sender:
            # Find the most recent message from this sender that has a profile pic
            prev_msg_with_pic = db.query(Message).filter(
                Message.conversation_id == conversation.id,
                Message.sender_name == sender,
                Message.sender_profile_pic.isnot(None)
            ).order_by(Message.id.desc()).first()
            
            if prev_msg_with_pic and prev_msg_with_pic.sender_profile_pic:
                effective_sender_pic = prev_msg_with_pic.sender_profile_pic
                logger.info(f"Bot {bot_profile_id}: Using profile pic from sender {sender}'s previous message in same group")

        if not effective_sender_pic and not is_group:
            # For private chats, use the conversation's profile pic for incoming messages
            effective_sender_pic = contact_profile_pic or conversation.profile_pic

        # For sender name in private chats, use the chat name if not explicitly provided
        effective_sender_name = sender
        if not effective_sender_name and not is_group:
            effective_sender_name = chat_name

        # Clean sender_whatsapp_id - remove @c.us/@g.us/@lid suffix, keep only ID
        clean_sender_id = _normalize_sender_id(sender_whatsapp_id)

        # Extract sender phone from sender_whatsapp_id if it's @c.us format (contains actual phone)
        sender_phone_clean = None
        if sender_whatsapp_id and '@c.us' in sender_whatsapp_id:
            phone_match = re.search(r'(\d{8,15})@c\.us', sender_whatsapp_id)
            if phone_match:
                sender_phone_clean = phone_match.group(1)

        # Parse the WhatsApp timestamp to get the actual message time
        # Get bot's timezone offset for conversion and ending detection setting
        msg_timestamp = None
        ending_detection_enabled = False  # Default to disabled
        bot_profile = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
        if bot_profile:
            ending_detection_enabled = bot_profile.ending_detection_enabled if bot_profile.ending_detection_enabled is not None else False
            if whatsapp_timestamp:
                timezone_offset = bot_profile.whatsapp_timezone_offset
                msg_timestamp = _parse_whatsapp_timestamp(whatsapp_timestamp, None, timezone_offset)

        # Create message with file info if available (skip if already synced)
        user_msg = None
        user_msg_data = None

        if not skip_message_store:
            user_msg = Message(
                conversation_id=conversation.id,
                role="user",
                content=message,
                sender_name=effective_sender_name,
                sender_id=clean_sender_id,  # Phone number only (e.g., "971524906816")
                sender_phone=sender_phone_clean,
                sender_profile_pic=effective_sender_pic,
                whatsapp_message_id=whatsapp_message_id,
                timestamp=msg_timestamp,  # Use parsed WhatsApp timestamp instead of DB default
                file_url=file_info.get('file_url') if file_info else None,
                file_type=file_info.get('file_type') if file_info else None,
                file_name=file_info.get('file_name') if file_info else None,
                file_size=file_info.get('file_size') if file_info else None,
                file_pages=file_info.get('file_pages') if file_info else None
            )
            db.add(user_msg)
            db.flush()  # Get the ID
            db.commit()  # Explicitly commit user message to avoid losing it in transaction conflicts

            if file_info:
                logger.info(f"Bot {bot_profile_id}: Saved incoming message with file: {file_info.get('file_url')}, type: {file_info.get('file_type')}, name: {file_info.get('file_name')}")

                # Analyze media (image or document) and store the analysis for future reference
                # Only analyze if we have a valid file_url (the file was successfully downloaded)
                file_url = file_info.get('file_url')
                if file_url:
                    try:
                        from pathlib import Path

                        # CRITICAL FIX: Use the actual local file path that was saved
                        # This ensures we analyze the exact file that was just saved
                        local_file_path = file_info.get('local_file_path')

                        if local_file_path:
                            file_path = local_file_path
                            logger.info(f"Bot {bot_profile_id}: Using local_file_path for analysis: {file_path}")
                        else:
                            # Fallback to reconstructing path from URL (for backwards compatibility)
                            file_path = str(_media_url_to_path(file_url))
                            logger.info(f"Bot {bot_profile_id}: Reconstructed file_path for analysis: {file_path}")

                        # Verify the file exists before analyzing
                        if not Path(file_path).exists():
                            logger.error(f"Bot {bot_profile_id}: File does not exist for analysis: {file_path}")
                        else:
                            file_size = Path(file_path).stat().st_size
                            logger.info(f"Bot {bot_profile_id}: File verified for analysis: {file_path} ({file_size} bytes)")

                        # Analyze media with AI (handles both images and documents)
                        analysis = _analyze_media_with_ai(
                            ai_provider,
                            file_path,
                            file_info.get('file_type'),
                            message  # Include user's message for context
                        )

                        if analysis:
                            user_msg.media_analysis = analysis
                            db.flush()
                            media_type = "image" if file_info.get('file_type', '').startswith('image/') else "document"
                            logger.info(f"Bot {bot_profile_id}: Stored {media_type} analysis: {analysis[:80]}...")
                    except Exception as analysis_err:
                        logger.error(f"Bot {bot_profile_id}: Error analyzing media: {analysis_err}")
                else:
                    # Document was detected but couldn't be downloaded - store a note
                    doc_type = _get_document_type_label(file_info.get('file_type', 'application/octet-stream'))
                    user_msg.media_analysis = f"A {doc_type} named '{file_info.get('file_name', 'document')}' was shared but could not be downloaded for analysis."
                    db.flush()
                    db.commit()  # Commit the media_analysis update
                    logger.info(f"Bot {bot_profile_id}: Document detected but not downloadable: {file_info.get('file_name')}")

            # Broadcast user message via WebSocket
            user_msg_data = {
                "id": user_msg.id,
                "role": "user",
                "content": message,
                "sender_name": effective_sender_name,
                "sender_profile_pic": effective_sender_pic,
                "timestamp": user_msg.timestamp.isoformat() + "Z",
                "file_url": file_info.get('file_url') if file_info else None,
                "file_type": file_info.get('file_type') if file_info else None,
                "file_name": file_info.get('file_name') if file_info else None,
                "file_size": file_info.get('file_size') if file_info else None,
                "file_pages": file_info.get('file_pages') if file_info else None
            }
        else:
            # Message already exists from sync - don't broadcast again
            logger.info(f"Bot {bot_profile_id}: Message already synced, not broadcasting duplicate")

        conv_id_for_broadcast = conversation.id

        # Prepare conversation data for chat list update
        conv_data_for_broadcast = {
            "id": conversation.id,
            "chat_id": conversation.chat_id,
            "chat_name": conversation.chat_name,
            "is_group": conversation.is_group,
            "profile_pic": conversation.profile_pic,
            "message_count": (conversation.message_count or 0) + 1,
            "last_message": message[:100],
            "last_message_at": datetime.utcnow().isoformat()
        }
        bot_id_for_broadcast = bot_profile_id

        # Get conversation history - use all messages for full context
        # max_history of 0 means unlimited, otherwise use the configured limit
        max_history = config.get('max_history', 100)  # Default to 100 messages
        if max_history == 0:
            # Get all messages
            history = db.query(Message).filter(
                Message.conversation_id == conversation.id
            ).order_by(Message.timestamp.asc()).all()
        else:
            # Get limited history, but use larger default for better AI context
            history = db.query(Message).filter(
                Message.conversation_id == conversation.id
            ).order_by(Message.timestamp.desc()).limit(max_history).all()
            history = list(reversed(history))

        logger.info(f"Bot {bot_profile_id}: Using {len(history)} messages for AI context")

        # Build messages list INSIDE the session to avoid detached instance errors
        # Use vision-capable message building for images

        # Build system prompt with natural conversation style
        base_prompt = config.get('system_prompt', 'You are a helpful assistant.')

        # Add current date/time to help AI know the actual date
        from datetime import datetime
        current_datetime = datetime.now()
        date_info = f"""

CURRENT DATE AND TIME:
- Today's date: {current_datetime.strftime('%A, %B %d, %Y')}
- Current time: {current_datetime.strftime('%I:%M %p')}
- Year: {current_datetime.year}
"""

        natural_style = """
IMPORTANT - Response Style:
- Write like a real person texting a friend - casual, warm, and natural
- Keep responses SHORT (1-3 sentences usually)
- Don't be overly formal or robotic
- Use simple everyday language
- Show genuine interest and personality
- React naturally to images - comment on what you see like a friend would
- Remember previous images and messages in the conversation for context
- Never use markdown formatting, asterisks, or special characters
- If you see multiple images in the conversation, reference them when relevant
- NEVER start your response with [Name]: or any similar prefix - just write the message directly
- The [Name]: prefixes in the conversation are just to show who said what - do NOT copy this format

REAL-TIME INFORMATION:
- You have access to web search to look up current information
- For questions about weather, news, current events, stock prices, sports scores, or anything requiring up-to-date info, use the web_search tool
- Always search when you're unsure or when the question requires current data
- Summarize search results naturally without mentioning you searched"""

        closing_instructions = """

CONVERSATION ENDING GUIDELINES:
- Recognize when a conversation has naturally concluded
- If the user sends a simple closing (thanks, bye, take care), respond BRIEFLY without asking follow-up questions that would extend the conversation
- Good closing responses: "You're welcome!", "Take care!", "Happy to help!"
- BAD closing responses: "You're welcome! Is there anything else I can help with?" (This invites continuation when the user wants to end)
- If you've already exchanged goodbyes once, do NOT respond to further pleasantries - let the conversation end naturally
- Match the user's energy - if they send a brief "bye", respond with a brief "bye", don't write a paragraph
- Look at the conversation history: if the last 2-3 exchanges are just short pleasantries (thanks, bye, take care), the conversation is OVER - do not respond"""

        full_system_prompt = base_prompt + date_info + natural_style + closing_instructions
        messages = [
            {"role": "system", "content": full_system_prompt}
        ]

        # Track media handling in history (images and documents)
        media_with_analysis = 0
        images_as_base64 = 0
        docs_without_analysis = 0
        for msg in history:
            # Use helper function - include_image=False means use stored analysis for history
            sender_prefix = msg.sender_name if msg.sender_name and msg.role == "user" else None
            built_msg = _build_openai_message(msg, sender_prefix, include_image=False)
            messages.append(built_msg)
            # Track media handling
            if msg.file_url and msg.file_type:
                if hasattr(msg, 'media_analysis') and msg.media_analysis:
                    media_with_analysis += 1
                elif msg.file_type.startswith('image/') and isinstance(built_msg.get('content'), list):
                    images_as_base64 += 1
                elif not msg.file_type.startswith('image/'):
                    docs_without_analysis += 1

        logger.info(f"Bot {bot_profile_id}: Built {len(history)} history messages - {media_with_analysis} media with analysis, {images_as_base64} images as base64, {docs_without_analysis} docs without analysis")

        last_msg_content = history[-1].content if history else None

        # Store flags for use outside db session
        is_human_takeover = conversation.human_takeover
        needs_history_sync = not conversation.history_synced
        conversation_db_id = conversation.id
        logger.info(f"Bot {bot_profile_id}: Conversation {chat_name} - human_takeover={is_human_takeover}")

    # Broadcast user message via WebSocket IMMEDIATELY (outside db session)
    # Skip if message was already synced from history (no new message data)
    try:
        from app.conversations.routes import conversation_ws_manager
        import asyncio

        # Use the stored main event loop from the WebSocket manager
        loop = conversation_ws_manager.get_main_loop()
        if loop and loop.is_running() and user_msg_data:
            # Broadcast the message to conversation viewers
            future = asyncio.run_coroutine_threadsafe(
                conversation_ws_manager.broadcast_message(conv_id_for_broadcast, user_msg_data),
                loop
            )
            # Wait briefly to ensure message is sent
            try:
                future.result(timeout=2)
            except Exception as e:
                logger.debug(f"Bot {bot_profile_id}: WebSocket broadcast wait: {e}")

            # Also broadcast chat list update (for profile pic and last message)
            future2 = asyncio.run_coroutine_threadsafe(
                conversation_ws_manager.broadcast_chat_update(bot_id_for_broadcast, conv_data_for_broadcast),
                loop
            )
            try:
                future2.result(timeout=2)
            except Exception as e:
                logger.debug(f"Bot {bot_profile_id}: WebSocket chat update wait: {e}")
        elif not user_msg_data:
            logger.debug(f"Bot {bot_profile_id}: Skipping WebSocket broadcast - message already synced from history")
        else:
            logger.warning(f"Bot {bot_profile_id}: Main event loop not available for WebSocket broadcast")

        if user_msg_data:
            logger.info(f"Bot {bot_profile_id}: Broadcast user message to WebSocket for conversation {conv_id_for_broadcast}")
    except Exception as ws_err:
        logger.warning(f"Bot {bot_profile_id}: Could not broadcast user message: {ws_err}", exc_info=True)

    # ==========================================================================
    # CENTRALIZED HUB PROCESSING (for group_management hubs)
    # For group messages in a group_management hub, use the centralized processor
    # which runs ALL AI calls (ending detection + classifier + router) ONCE
    # and distributes decisions to all bots.
    # ==========================================================================
    hub_decision = None
    hub_execution_id = None
    routing_context = None
    other_responders = []
    coordination_hint = ''
    use_centralized_processor = False

    if is_group:
        try:
            from app.hubs.coordinator import get_hub_message_decision, get_hub_for_bot_and_group, update_hub_execution_response
            from app.database import ActivityLog
            import re

            # Check if this bot is in a group_management hub for this specific group
            # This handles bots that belong to multiple hubs managing different groups
            with get_db_session() as db:
                chat_id_to_check = chat_data_id or chat_name
                hub = get_hub_for_bot_and_group(bot_profile_id, chat_id_to_check, db)
                logger.debug(f"Bot {bot_profile_id}: Hub check for group {chat_id_to_check} - hub={hub.id if hub else None}")
                if hub:
                    # Hub found that manages this bot+group combination
                    use_centralized_processor = True

                    # Extract sender phone
                    sender_phone = None
                    sender_display_name = sender
                    if sender_whatsapp_id and '@c.us' in sender_whatsapp_id:
                        phone_match = re.search(r'(\d{8,15})@c\.us', sender_whatsapp_id)
                        if phone_match:
                            sender_phone = phone_match.group(1)

                    # Get centralized decision (runs ALL AI calls once)
                    hub_decision = get_hub_message_decision(
                        hub_id=hub.id,
                        bot_id=bot_profile_id,
                        chat_id=chat_data_id or chat_name,
                        message_content=message,
                        sender_id=clean_sender_id,
                        sender_name=sender_display_name,
                        sender_phone=sender_phone,
                        is_group=True,
                        whatsapp_message_id=whatsapp_message_id,
                        conversation_id=conversation_db_id
                    )

                    logger.info(f"Bot {bot_profile_id}: Centralized hub decision: should_respond={hub_decision.get('should_respond')}, reason={hub_decision.get('reason')}")

                    # Capture execution_id for response logging
                    hub_execution_id = hub_decision.get('execution_id')
                    if hub_execution_id:
                        logger.debug(f"Bot {bot_profile_id}: Hub execution ID: {hub_execution_id}")

                    if not hub_decision.get('should_respond', True):
                        # Hub says don't respond - could be ending detection OR routing
                        logger.info(f"Bot {bot_profile_id}: Hub processor says NOT to respond - {hub_decision.get('reason')}")
                        with get_db_session() as db2:
                            conversation = db2.query(Conversation).filter(
                                Conversation.bot_profile_id == bot_profile_id,
                                Conversation.chat_name == chat_name
                            ).first()
                            if conversation:
                                conversation.message_count = (conversation.message_count or 0) + 1
                                conversation.last_message_at = datetime.utcnow()

                            # Log activity
                            log = ActivityLog(
                                bot_profile_id=bot_profile_id,
                                action="hub_routing_skip",
                                details=f"Skipped response to '{chat_name}' - {hub_decision.get('reason')}"
                            )
                            db2.add(log)
                        return

                    # Extract routing context for response enhancement
                    routing_context = hub_decision.get('routing_context')
                    coordination_hint = hub_decision.get('coordination_hint', '')

                    # Apply delay if specified
                    delay_ms = hub_decision.get('delay_ms', 0)
                    if delay_ms > 0:
                        logger.info(f"Bot {bot_profile_id}: Hub processor specified delay of {delay_ms}ms")
                        time.sleep(delay_ms / 1000.0)

        except ImportError as ie:
            logger.debug(f"Bot {bot_profile_id}: Hub processor not available: {ie}")
            use_centralized_processor = False
        except Exception as hub_proc_err:
            logger.warning(f"Bot {bot_profile_id}: Centralized hub processing failed: {hub_proc_err}, falling back to standard flow")
            use_centralized_processor = False
            hub_decision = None

    # ==========================================================================
    # STANDARD FLOW (for non-hub messages or if centralized processor not used)
    # ==========================================================================
    if not use_centralized_processor:
        # [1] Check if conversation is naturally ending (BEFORE bot-to-bot detection)
        # This prevents endless farewell loops like "Bye!" -> "Bye!" -> "Take care!" -> "You too!"
        try:
            from app.conversations.ending_detector import should_skip_for_conversation_ending
            from app.database import ActivityLog

            if should_skip_for_conversation_ending(
                ai_provider=ai_provider,
                conversation_id=conversation_db_id,
                message_content=message,
                bot_profile_id=bot_profile_id,
                is_group=is_group,
                sender_name=sender if is_group else None,
                sender_id=clean_sender_id,
                chat_id=chat_data_id,  # For caching across bots
                use_ai=ending_detection_enabled  # Use AI only if bot setting enabled
            ):
                logger.info(f"Bot {bot_profile_id}: Conversation ending detected for {chat_name}, skipping AI response")
                # Update message count and log activity (no AI response)
                with get_db_session() as db:
                    conversation = db.query(Conversation).filter(
                        Conversation.bot_profile_id == bot_profile_id,
                        Conversation.chat_name == chat_name
                    ).first()
                    if conversation:
                        conversation.message_count = (conversation.message_count or 0) + 1
                        conversation.last_message_at = datetime.utcnow()

                    # Log to activity for visibility in dashboard
                    log = ActivityLog(
                        bot_profile_id=bot_profile_id,
                        action="conversation_ending_detected",
                        details=f"Skipped response to '{chat_name}' - conversation naturally ending"
                    )
                    db.add(log)
                return
        except ImportError:
            logger.debug(f"Bot {bot_profile_id}: Conversation ending detector not available")
        except Exception as ending_err:
            logger.warning(f"Bot {bot_profile_id}: Conversation ending check failed: {ending_err}, proceeding with response")

    # [2] Skip AI response if this is not the last message in a batch (for multiple incoming messages)
    if skip_ai_response:
        logger.info(f"Bot {bot_profile_id}: Skipping AI response for {chat_name} (not last message in batch)")
        return

    # Check if human has taken over this conversation - skip AI response if so
    if is_human_takeover:
        logger.info(f"Bot {bot_profile_id}: Human takeover active for {chat_name}, skipping AI response. Click 'Resume AI' to enable bot responses.")
        # Update message count only (no AI response)
        with get_db_session() as db:
            conversation = db.query(Conversation).filter(
                Conversation.bot_profile_id == bot_profile_id,
                Conversation.chat_name == chat_name
            ).first()
            if conversation:
                conversation.message_count = (conversation.message_count or 0) + 1
                conversation.last_message_at = datetime.utcnow()
        return

    # Check hub routing (for non-group_management hubs or fallback)
    # Skip if we already have a decision from centralized processor
    if not use_centralized_processor:
        try:
            from app.hubs.coordinator import check_hub_routing, update_hub_execution_response
            import re

            # Extract sender phone for hub contact tracking
            sender_phone = None
            sender_display_name = sender  # The display name from message extraction

            if is_group and sender_whatsapp_id:
                # Group chat - extract phone from sender's WhatsApp ID
                # ONLY use @c.us format (contains actual phone), NOT @lid format
                if '@c.us' in sender_whatsapp_id:
                    phone_match = re.search(r'(\d{8,15})@c\.us', sender_whatsapp_id)
                    if phone_match:
                        sender_phone = phone_match.group(1)
            else:
                # Private chat - look up the conversation to get the correct phone
                # The conversation.phone field has the properly extracted phone number
                with get_db_session() as db:
                    conv = db.query(Conversation).filter(
                        Conversation.bot_profile_id == bot_profile_id,
                        Conversation.chat_name == chat_name
                    ).first()
                    if conv:
                        if conv.phone:
                            sender_phone = conv.phone
                        # Use display_name or chat_name for the contact name
                        if not sender_display_name:
                            sender_display_name = conv.display_name or conv.chat_name

                # Fallback: try to extract from @c.us format chat_data_id (not @lid)
                if not sender_phone and chat_data_id and '@c.us' in chat_data_id:
                    phone_match = re.search(r'(\d{8,15})@c\.us', chat_data_id)
                    if phone_match:
                        sender_phone = phone_match.group(1)

            # For private chats, use chat_name as display name if still not set
            if not is_group and not sender_display_name:
                sender_display_name = chat_name

            if not sender_phone:
                logger.debug(f"Bot {bot_profile_id}: Could not extract phone for hub contact (is_group={is_group}, chat_data_id={chat_data_id})")

            hub_decision = check_hub_routing(
                bot_id=bot_profile_id,
                message_content=message,
                sender_phone=sender_phone,  # Can be None - coordinator will handle
                sender_name=sender_display_name,  # Pass name separately
                chat_id=chat_data_id or chat_name,
                is_group=is_group,
                whatsapp_message_id=whatsapp_message_id  # For bot-to-bot detection by message ID
            )

            # Store execution_id for response logging
            hub_execution_id = hub_decision.get('execution_id')

            if not hub_decision.get('should_respond', True):
                logger.info(f"Bot {bot_profile_id}: Hub routing says NOT to respond - reason: {hub_decision.get('reason')}")
                # Update message count only (no AI response)
                with get_db_session() as db:
                    conversation = db.query(Conversation).filter(
                        Conversation.bot_profile_id == bot_profile_id,
                        Conversation.chat_name == chat_name
                    ).first()
                    if conversation:
                        conversation.message_count = (conversation.message_count or 0) + 1
                        conversation.last_message_at = datetime.utcnow()
                return
            else:
                logger.info(f"Bot {bot_profile_id}: Hub routing says OK to respond - reason: {hub_decision.get('reason')}")

                # Apply delay if specified by hub router
                delay_ms = hub_decision.get('delay_ms', 0)
                if delay_ms > 0:
                    logger.info(f"Bot {bot_profile_id}: Hub router specified delay of {delay_ms}ms")
                    time.sleep(delay_ms / 1000.0)

                # Extract routing context
                routing_context = hub_decision.get('routing_context')
                other_responders = hub_decision.get('other_responders', [])
                coordination_hint = hub_decision.get('coordination_hint', '')

        except ImportError:
            logger.debug(f"Bot {bot_profile_id}: Hub module not available, proceeding with normal response")
        except Exception as hub_err:
            logger.warning(f"Bot {bot_profile_id}: Hub routing check failed: {hub_err}, proceeding with normal response")

    if routing_context:
        logger.info(f"Bot {bot_profile_id}: Routing context available - {routing_context.get('why_selected', 'unknown')}")

    logger.info(f"Bot {bot_profile_id}: AI enabled for {chat_name}, generating response...")

    # On-demand sync: If this conversation hasn't had history synced, do it now before AI response
    if needs_history_sync:
        logger.info(f"Bot {bot_profile_id}: Syncing history on-demand for {chat_name} before AI response")
        _sync_conversation_history_on_demand(page, bot_profile_id, conversation_db_id, chat_name)

        # Re-fetch conversation history after sync
        with get_db_session() as db:
            max_history = config.get('max_history', 100)
            if max_history == 0:
                history = db.query(Message).filter(
                    Message.conversation_id == conversation_db_id
                ).order_by(Message.timestamp.asc()).all()
            else:
                history = db.query(Message).filter(
                    Message.conversation_id == conversation_db_id
                ).order_by(Message.timestamp.desc()).limit(max_history).all()
                history = list(reversed(history))

            logger.info(f"Bot {bot_profile_id}: After on-demand sync, using {len(history)} messages for AI context")

            # Rebuild messages list with synced history
            # Use vision-capable message building for images

            # Build system prompt with natural conversation style
            base_prompt = config.get('system_prompt', 'You are a helpful assistant.')
            natural_style = """

IMPORTANT - Response Style:
- Write like a real person texting a friend - casual, warm, and natural
- Keep responses SHORT (1-3 sentences usually)
- Don't be overly formal or robotic
- Use simple everyday language
- Show genuine interest and personality
- React naturally to images - comment on what you see like a friend would
- Remember previous images and messages in the conversation for context
- Never use markdown formatting, asterisks, or special characters
- If you see multiple images in the conversation, reference them when relevant
- NEVER start your response with [Name]: or any similar prefix - just write the message directly
- The [Name]: prefixes in the conversation are just to show who said what - do NOT copy this format"""

            closing_instructions = """

CONVERSATION ENDING GUIDELINES:
- Recognize when a conversation has naturally concluded
- If the user sends a simple closing (thanks, bye, take care), respond BRIEFLY without asking follow-up questions that would extend the conversation
- Good closing responses: "You're welcome!", "Take care!", "Happy to help!"
- BAD closing responses: "You're welcome! Is there anything else I can help with?" (This invites continuation when the user wants to end)
- If you've already exchanged goodbyes once, do NOT respond to further pleasantries - let the conversation end naturally
- Match the user's energy - if they send a brief "bye", respond with a brief "bye", don't write a paragraph
- Look at the conversation history: if the last 2-3 exchanges are just short pleasantries (thanks, bye, take care), the conversation is OVER - do not respond"""

            full_system_prompt = base_prompt + natural_style + closing_instructions

            # Note: Routing context is injected later, just before the AI call,
            # to handle both code paths (with and without needs_history_sync)

            messages = [
                {"role": "system", "content": full_system_prompt}
            ]

            # Track media handling in history (images and documents)
            media_with_analysis = 0
            images_as_base64 = 0
            docs_without_analysis = 0
            for msg in history:
                # Use helper function - include_image=False means use stored analysis for history
                sender_prefix = msg.sender_name if msg.sender_name and msg.role == "user" else None
                built_msg = _build_openai_message(msg, sender_prefix, include_image=False)
                messages.append(built_msg)
                # Track media handling
                if msg.file_url and msg.file_type:
                    if hasattr(msg, 'media_analysis') and msg.media_analysis:
                        media_with_analysis += 1
                    elif msg.file_type.startswith('image/') and isinstance(built_msg.get('content'), list):
                        images_as_base64 += 1
                    elif not msg.file_type.startswith('image/'):
                        docs_without_analysis += 1

            logger.info(f"Bot {bot_profile_id}: Built {len(history)} history messages - {media_with_analysis} media with analysis, {images_as_base64} images as base64, {docs_without_analysis} docs without analysis")

            last_msg_content = history[-1].content if history else None

    if not history or last_msg_content != message:
        # For the current message, check if it has an image (file_info)
        msg_content = f"[{sender}]: {message}" if sender else message
        logger.info(f"Bot {bot_profile_id}: Adding current message to AI context - file_info={file_info is not None}, file_type={file_info.get('file_type') if file_info else 'N/A'}")
        if file_info and file_info.get('file_type', '').startswith('image/'):
            # Build multi-part message with image
            import base64 as b64
            from pathlib import Path
            try:
                file_path = _media_url_to_path(file_info.get('file_url', ''))
                logger.info(f"Bot {bot_profile_id}: Current image - file_url={file_info.get('file_url')}, file_path={file_path}")
                if file_path.exists():
                    with open(file_path, 'rb') as f:
                        image_data = f.read()
                    image_base64 = b64.b64encode(image_data).decode('utf-8')
                    text_content = msg_content if msg_content else "Please describe or respond to this image."
                    logger.info(f"Bot {bot_profile_id}: Adding current image to AI (base64 length: {len(image_base64)})")
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": text_content},
                            {"type": "image_url", "image_url": {"url": f"data:{file_info.get('file_type')};base64,{image_base64}", "detail": "auto"}}
                        ]
                    })
                else:
                    logger.warning(f"Bot {bot_profile_id}: Current image NOT FOUND at {file_path}")
                    messages.append({"role": "user", "content": msg_content})
            except Exception as img_err:
                logger.error(f"Bot {bot_profile_id}: Error adding current image to AI context: {img_err}")
                messages.append({"role": "user", "content": msg_content})
        else:
            messages.append({"role": "user", "content": msg_content})

    # Broadcast typing indicator (start) - wait to ensure it arrives before the response
    try:
        from app.conversations.routes import conversation_ws_manager
        import asyncio

        # Use the stored main event loop from the WebSocket manager
        loop = conversation_ws_manager.get_main_loop()
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                conversation_ws_manager.broadcast_typing(conv_id_for_broadcast, True, "Bot"),
                loop
            )
            try:
                future.result(timeout=2)
            except Exception as e:
                logger.debug(f"Bot {bot_profile_id}: Typing start broadcast wait: {e}")
        logger.info(f"Bot {bot_profile_id}: Broadcast typing start to WebSocket")
    except Exception as ws_err:
        logger.warning(f"Bot {bot_profile_id}: Could not broadcast typing start: {ws_err}")

    try:
        # Debug: Log message structure before sending to OpenAI
        logger.info(f"Bot {bot_profile_id}: Sending {len(messages)} messages to OpenAI")
        for i, msg in enumerate(messages):
            role = msg.get('role', 'unknown')
            content = msg.get('content', '')
            if isinstance(content, list):
                # Multi-part message (has image)
                parts_desc = []
                for part in content:
                    if part.get('type') == 'text':
                        parts_desc.append(f"text({len(part.get('text', ''))} chars)")
                    elif part.get('type') == 'image_url':
                        url = part.get('image_url', {}).get('url', '')
                        if url.startswith('data:'):
                            parts_desc.append(f"image_url(base64, ~{len(url)} chars)")
                        else:
                            parts_desc.append(f"image_url({url[:50]}...)")
                logger.info(f"  [{i}] {role}: MULTI-PART with {parts_desc}")
            else:
                logger.info(f"  [{i}] {role}: {str(content)[:80]}...")

        # Inject routing context into system prompt if available (for hub-managed group conversations)
        # This handles both code paths (with and without needs_history_sync)
        if routing_context and messages and len(messages) > 0:
            original_system_prompt = messages[0].get('content', '')

            hub_context = f"""

HUB COORDINATION CONTEXT:
- Why you're responding: {routing_context.get('why_selected', 'Selected to respond')}
- Topic category: {routing_context.get('classification', {}).get('category', 'general')}
- Conversation depth: {routing_context.get('conversation_depth', 0)} previous exchanges"""

            if routing_context.get('is_continuation'):
                hub_context += """
- This is a CONTINUATION of your conversation with this person - maintain the natural flow"""

            if routing_context.get('coordination_hint'):
                hub_context += f"""
- Coordination guidance: {routing_context.get('coordination_hint')}"""

            # Add multi-bot coordination if applicable
            if other_responders:
                other_names = ", ".join([r.get('bot_name', 'Another bot') for r in other_responders])
                hub_context += f"""

MULTI-BOT COORDINATION:
- Other bots also responding: {other_names}
- Keep your response focused and avoid redundancy"""
                if coordination_hint:
                    hub_context += f"""
- Your role: {coordination_hint}"""

            messages[0]['content'] = original_system_prompt + hub_context
            logger.info(f"Bot {bot_profile_id}: Injected hub routing context into system prompt")

        # Use tool-enabled AI response for real-time queries (weather, news, etc.)
        from app.bots.web_search import process_ai_response_with_tools

        reply = process_ai_response_with_tools(
            ai_provider=ai_provider,
            messages=messages,
            model=config.get('model', 'gpt-4o-mini'),
            max_tokens=config.get('max_tokens', 1000),
            temperature=config.get('temperature', 0.7),
            top_p=config.get('top_p', 1.0),
            frequency_penalty=config.get('frequency_penalty', 0.0),
            presence_penalty=config.get('presence_penalty', 0.0)
        )

        # Strip markdown formatting from AI response
        # This ensures clean text for WhatsApp even if AI ignores the system prompt
        reply = _strip_markdown_formatting(reply)

        # Strip [Name]: prefix if AI copied the conversation format
        reply = _strip_sender_prefix(reply)

        # Log response to hub execution if tracking
        if hub_execution_id:
            try:
                logger.info(f"Bot {bot_profile_id}: Updating hub execution {hub_execution_id} with response: {reply[:50]}...")
                success = update_hub_execution_response(hub_execution_id, reply)
                if success:
                    logger.info(f"Bot {bot_profile_id}: Successfully logged response to hub execution {hub_execution_id}")
                else:
                    logger.warning(f"Bot {bot_profile_id}: update_hub_execution_response returned False for execution {hub_execution_id}")
            except Exception as log_err:
                logger.warning(f"Bot {bot_profile_id}: Failed to log hub execution response: {log_err}")
        else:
            logger.debug(f"Bot {bot_profile_id}: No hub_execution_id available for response logging")
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply = "Sorry, I encountered an error. Please try again."

    # Broadcast typing indicator (stop) - wait to ensure proper ordering
    try:
        # Use the stored main event loop from the WebSocket manager
        loop = conversation_ws_manager.get_main_loop()
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                conversation_ws_manager.broadcast_typing(conv_id_for_broadcast, False, "Bot"),
                loop
            )
            try:
                future.result(timeout=2)
            except Exception as e:
                logger.debug(f"Bot {bot_profile_id}: Typing stop broadcast wait: {e}")
        logger.info(f"Bot {bot_profile_id}: Broadcast typing stop to WebSocket")
    except Exception as ws_err:
        logger.warning(f"Bot {bot_profile_id}: Could not broadcast typing stop: {ws_err}")

    # Variables for WebSocket broadcast
    conversation_id = None
    assistant_msg_data = None

    with get_db_session() as db:
        conversation = db.query(Conversation).filter(
            Conversation.bot_profile_id == bot_profile_id,
            Conversation.chat_name == chat_name
        ).first()

        if conversation:
            # Get bot profile to use bot's WhatsApp ID and profile pic for assistant messages
            from app.database import BotProfile
            bot_profile = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()

            # Use bot's WhatsApp info if available, otherwise fall back to defaults
            bot_whatsapp_id = None
            bot_profile_pic = AI_AGENT_PROFILE_PIC  # Default fallback

            if bot_profile:
                # Use phone number only (without @c.us suffix) for sender_id
                if bot_profile.whatsapp_phone:
                    bot_whatsapp_id = bot_profile.whatsapp_phone.replace('+', '').replace(' ', '').replace('-', '')

                # Use bot's WhatsApp profile pic if available
                if bot_profile.whatsapp_profile_pic:
                    bot_profile_pic = bot_profile.whatsapp_profile_pic

            assistant_msg = Message(
                conversation_id=conversation.id,
                role="assistant",
                content=reply,
                sender_name="AI Agent",
                sender_id=bot_whatsapp_id,  # Phone number only (e.g., "96599328119")
                sender_profile_pic=bot_profile_pic
            )
            db.add(assistant_msg)
            db.flush()  # Get the ID
            conversation.message_count = (conversation.message_count or 0) + 2
            conversation.last_message_at = datetime.utcnow()

            # Prepare data for WebSocket broadcast
            conversation_id = conversation.id
            assistant_msg_data = {
                "id": assistant_msg.id,
                "role": "assistant",
                "content": reply,
                "sender_name": "AI Agent",
                "sender_id": bot_whatsapp_id,
                "sender_profile_pic": bot_profile_pic,
                "timestamp": assistant_msg.timestamp.isoformat() + "Z"
            }

        log = ActivityLog(
            bot_profile_id=bot_profile_id,
            action="message_sent",
            details=f"To: {chat_name}"
        )
        db.add(log)

    # Broadcast message via WebSocket (outside db session) - wait to ensure it arrives after typing stops
    if conversation_id and assistant_msg_data:
        try:
            from app.conversations.routes import conversation_ws_manager
            import asyncio

            # Use the stored main event loop from the WebSocket manager
            loop = conversation_ws_manager.get_main_loop()
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    conversation_ws_manager.broadcast_message(conversation_id, assistant_msg_data),
                    loop
                )
                try:
                    future.result(timeout=2)
                except Exception as e:
                    logger.debug(f"Bot {bot_profile_id}: Assistant message broadcast wait: {e}")
            logger.info(f"Bot {bot_profile_id}: Broadcast message to WebSocket for conversation {conversation_id}")
        except Exception as ws_err:
            logger.warning(f"Bot {bot_profile_id}: Could not broadcast message: {ws_err}")

    delay_min = config.get('response_delay_min', 3)
    delay_max = config.get('response_delay_max', 8)
    delay = random.uniform(delay_min, delay_max)
    time.sleep(delay)

    try:
        # Try multiple selectors for the input box (WhatsApp Business compatible)
        input_selectors = [
            '[data-testid="conversation-compose-box-input"]',
            '[contenteditable="true"][data-tab="10"]',
            'div[contenteditable="true"][role="textbox"]',
            'footer [contenteditable="true"]',
            '[aria-placeholder="Type a message"]',
            'div[title="Type a message"]'
        ]

        input_box = None
        for selector in input_selectors:
            try:
                input_box = page.wait_for_selector(selector, timeout=2000)
                if input_box:
                    logger.info(f"Bot {bot_profile_id}: Found input with selector: {selector}")
                    break
            except:
                continue

        if not input_box:
            logger.error(f"Bot {bot_profile_id}: Could not find message input box")
            return

        # Click to focus, then type the message
        input_box.click()
        time.sleep(0.2)
        input_box.fill(reply)
        time.sleep(0.3)

        # Try multiple selectors for send button
        send_selectors = [
            '[data-testid="send"]',
            '[data-icon="send"]',
            'button[aria-label="Send"]',
            'span[data-icon="send"]'
        ]

        send_button = None
        for selector in send_selectors:
            send_button = page.query_selector(selector)
            if send_button:
                break

        if send_button:
            send_button.click()
        else:
            # Fallback: press Enter to send
            input_box.press('Enter')

        logger.info(f"Bot {bot_profile_id}: Sent to {chat_name}: {reply[:50]}...")

        # Wait a moment for the message to be sent and appear in DOM
        time.sleep(1.5)

        # Extract the WhatsApp message ID from the most recent outgoing message
        # This is needed for reliable bot-to-bot detection across hubs
        try:
            sent_msg_id = page.evaluate('''() => {
                // Find all outgoing messages (message-out class or data-id starting with "true_")
                const outMsgs = document.querySelectorAll('.message-out[data-id], [data-id^="true_"]');
                if (outMsgs.length === 0) return null;

                // Get the last (most recent) outgoing message
                const lastOut = outMsgs[outMsgs.length - 1];
                const dataId = lastOut.getAttribute('data-id');
                if (!dataId) return null;

                // Extract the message ID portion from data-id
                // Format: "true_PHONE@c.us_MSGID" or "true_GROUPID@g.us_MSGID_SENDERID@lid"
                const parts = dataId.split('_');
                if (parts.length >= 3) {
                    // The message ID is typically the 3rd part (index 2)
                    return parts[2];
                }
                return null;
            }''')

            if sent_msg_id:
                logger.info(f"Bot {bot_profile_id}: Captured sent message ID: {sent_msg_id}")
                # Update the stored message with the WhatsApp message ID
                with get_db_session() as db:
                    # Find the most recent assistant message for this conversation
                    from app.database import Message, Conversation
                    conv = db.query(Conversation).filter(
                        Conversation.bot_profile_id == bot_profile_id,
                        Conversation.chat_name == chat_name
                    ).first()
                    if conv:
                        last_assistant_msg = db.query(Message).filter(
                            Message.conversation_id == conv.id,
                            Message.role == 'assistant'
                        ).order_by(Message.timestamp.desc()).first()
                        if last_assistant_msg:
                            last_assistant_msg.whatsapp_message_id = sent_msg_id
                            logger.info(f"Bot {bot_profile_id}: Updated message {last_assistant_msg.id} with whatsapp_message_id={sent_msg_id}")
            else:
                logger.debug(f"Bot {bot_profile_id}: Could not extract sent message ID from DOM")
        except Exception as msg_id_err:
            logger.warning(f"Bot {bot_profile_id}: Failed to capture sent message ID: {msg_id_err}")

        # Navigate away from the chat so new messages will appear as unread
        # Press Escape to deselect, then click on chat list header area
        try:
            page.keyboard.press('Escape')
            time.sleep(0.3)

            # Try to click on a neutral area (search box or header) to deselect chat
            neutral_selectors = [
                '[data-testid="chat-list-search"]',
                '[aria-label="Search input textbox"]',
                'header',
                '[data-testid="chatlist-header"]'
            ]
            for selector in neutral_selectors:
                neutral = page.query_selector(selector)
                if neutral:
                    neutral.click()
                    logger.info(f"Bot {bot_profile_id}: Clicked away from chat to enable unread detection")
                    break
        except Exception as nav_err:
            logger.warning(f"Bot {bot_profile_id}: Could not navigate away from chat: {nav_err}")

    except Exception as e:
        logger.error(f"Error sending message: {e}")


def _sync_all_conversations_and_messages(page, bot_profile_id, notify_status):
    """
    Sync ALL conversations and messages from WhatsApp Web to the database.
    This extracts:
    - All conversations from the sidebar
    - All messages from each conversation
    - Profile pictures for each contact
    """
    from app.database import get_db_session, Conversation, Message, BotProfile
    import time

    # Get bot's max_history setting and timezone offset
    max_history = 20  # Default
    timezone_offset = None  # Will be detected from first new message if not set
    with get_db_session() as db:
        bot = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
        if bot:
            max_history = bot.max_history or 20
            timezone_offset = bot.whatsapp_timezone_offset  # May be None if not yet detected

    logger.info(f"Bot {bot_profile_id}: Starting full conversation sync from WhatsApp Web (max_history={max_history}, tz_offset={timezone_offset})...")
    notify_status({"status": "syncing", "message": "Syncing conversations from WhatsApp..."})

    # Give sidebar time to fully load
    time.sleep(3)

    # Step 1: Get all conversations from sidebar with their info
    logger.info(f"Bot {bot_profile_id}: Extracting all conversations from sidebar...")
    sidebar_data = page.evaluate('''async () => {
        const conversations = [];

        // Find all chat rows in sidebar
        const chatRows = document.querySelectorAll('#pane-side [role="listitem"], #pane-side [role="row"], [data-testid="cell-frame-container"]');

        for (const row of chatRows) {
            // Try multiple selectors to find the contact name
            const nameSpan = row.querySelector('span[title][dir="auto"]._ao3e') ||
                            row.querySelector('span._ao3e[title]') ||
                            row.querySelector('span[data-testid="cell-frame-title"] span[title]') ||
                            row.querySelector('[data-testid="cell-frame-title"] span[title]') ||
                            row.querySelector('span.ggj6brxn[title]') ||
                            row.querySelector('span[title][dir="auto"]') ||
                            row.querySelector('span[title]');

            // Get both title attribute and text content
            const titleAttr = nameSpan ? nameSpan.getAttribute('title') : null;
            const textContent = nameSpan ? nameSpan.textContent?.trim() : null;

            // Determine which is the name and which is the phone
            const isTextPhone = textContent && /^[\\+\\d][\\d\\s\\-\\(\\)]{7,}$/.test(textContent);
            const isTitlePhone = titleAttr && /^[\\+\\d][\\d\\s\\-\\(\\)]{7,}$/.test(titleAttr);

            // Prioritize the one that looks like a name (not a phone number)
            let chatName;
            if (textContent && !isTextPhone) {
                chatName = textContent;
            } else if (titleAttr && !isTitlePhone) {
                chatName = titleAttr;
            } else {
                chatName = titleAttr || textContent;
            }

            if (!chatName) continue;

            // Extract FULL data-id - use specific methods to avoid wrong matches
            let fullDataId = null;

            // Method 1: Check row itself
            let dataId = row.getAttribute('data-id');
            if (dataId && (dataId.includes('@c.us') || dataId.includes('@g.us'))) {
                fullDataId = dataId;
            }

            // Method 2: Direct child with data-id
            if (!fullDataId) {
                const rowChild = row.querySelector(':scope > div[data-id]');
                if (rowChild) {
                    const id = rowChild.getAttribute('data-id');
                    if (id && (id.includes('@c.us') || id.includes('@g.us'))) {
                        fullDataId = id;
                    }
                }
            }

            // Method 3: First-level children
            if (!fullDataId) {
                for (const child of row.children) {
                    const id = child.getAttribute('data-id');
                    if (id && (id.includes('@c.us') || id.includes('@g.us'))) {
                        fullDataId = id;
                        break;
                    }
                }
            }

            // Method 4: Any data-id within row (but NOT ancestors)
            if (!fullDataId) {
                const dataIdElements = row.querySelectorAll('[data-id]');
                for (const elem of dataIdElements) {
                    const id = elem.getAttribute('data-id');
                    if (id && (id.includes('@c.us') || id.includes('@g.us'))) {
                        fullDataId = id;
                        break;
                    }
                }
            }

            // Check if it's a group - data-id suffix is SOURCE OF TRUTH
            const avatarArea = row.querySelector('[data-testid="cell-frame-primary"]') || row;
            const hasGroupIcon = avatarArea.querySelector('[data-icon="default-group"]') !== null ||
                                avatarArea.querySelector('[data-testid="default-group"]') !== null;

            let isGroup = false;
            if (fullDataId) {
                isGroup = fullDataId.includes('@g.us');
            } else {
                isGroup = hasGroupIcon;
            }

            // Get profile picture
            let profilePic = null;
            const img = row.querySelector('img[draggable="false"]');
            if (img && img.src && !img.src.includes('default-user') && !img.src.includes('default-group')) {
                try {
                    const response = await fetch(img.src);
                    const blob = await response.blob();
                    profilePic = await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result);
                        reader.onerror = reject;
                        reader.readAsDataURL(blob);
                    });
                } catch(e) {}
            }

            // Get last message preview and time
            const lastMsgEl = row.querySelector('[data-testid="last-msg-status"]')?.parentElement ||
                             row.querySelector('span[dir="ltr"]');
            const lastMsg = lastMsgEl ? lastMsgEl.innerText : null;

            conversations.push({
                name: chatName,
                fullDataId: fullDataId,
                isGroup: isGroup,
                profilePic: profilePic,
                lastMessage: lastMsg
            });
        }

        return conversations;
    }''')

    logger.info(f"Bot {bot_profile_id}: Found {len(sidebar_data)} conversations in WhatsApp sidebar")

    # Step 2: Store conversations and extract messages from each
    synced_count = 0
    total_messages = 0

    with get_db_session() as db:
        for idx, conv_info in enumerate(sidebar_data):
            chat_name = conv_info.get('name')
            chat_data_id = conv_info.get('fullDataId')
            is_group = conv_info.get('isGroup', False)
            profile_pic = conv_info.get('profilePic')

            if not chat_name:
                continue

            # Use full data-id as chat_id ONLY if valid, otherwise we'll look for existing
            chat_id = chat_data_id if _is_valid_whatsapp_id(chat_data_id) else None

            conversation = None

            # First, try to find by valid chat_id
            if chat_id:
                conversation = db.query(Conversation).filter(
                    Conversation.bot_profile_id == bot_profile_id,
                    Conversation.chat_id == chat_id
                ).first()

            # If not found, search by name for any existing conversation with valid WhatsApp ID
            if not conversation:
                all_matching = db.query(Conversation).filter(
                    Conversation.bot_profile_id == bot_profile_id,
                    Conversation.chat_name == chat_name
                ).all()

                # Prefer conversations with valid WhatsApp IDs
                for existing in all_matching:
                    if _is_valid_whatsapp_id(existing.chat_id):
                        conversation = existing
                        break

                # If no valid-ID conversation, use any existing
                if not conversation and all_matching:
                    conversation = all_matching[0]

            # Only create new conversation if we have a valid WhatsApp ID
            if not conversation:
                if not _is_valid_whatsapp_id(chat_data_id):
                    logger.warning(f"Bot {bot_profile_id}: Skipping conversation '{chat_name}' - no valid WhatsApp ID (got: {chat_data_id})")
                    continue  # Skip this conversation - don't create with invalid ID

                conversation = Conversation(
                    bot_profile_id=bot_profile_id,
                    chat_id=chat_data_id,  # Use validated chat_data_id
                    chat_name=chat_name,
                    is_group=is_group,
                    profile_pic=profile_pic
                )
                db.add(conversation)
                db.flush()
                logger.info(f"Bot {bot_profile_id}: Created conversation: {chat_name} (ID: {chat_data_id})")
            else:
                # Update existing conversation with full data-id ONLY if valid
                if _is_valid_whatsapp_id(chat_data_id) and conversation.chat_id != chat_data_id:
                    logger.info(f"Bot {bot_profile_id}: Updating chat_id from '{conversation.chat_id}' to valid WhatsApp ID '{chat_data_id}'")
                    conversation.chat_id = chat_data_id
                if profile_pic and not conversation.profile_pic:
                    conversation.profile_pic = profile_pic
                db.flush()

            synced_count += 1

            # Update status periodically
            if idx % 5 == 0:
                notify_status({
                    "status": "syncing",
                    "message": f"Syncing conversation {idx + 1}/{len(sidebar_data)}: {chat_name[:20]}..."
                })

        db.commit()

    logger.info(f"Bot {bot_profile_id}: Synced {synced_count} conversations")

    # Step 3: Extract messages from each conversation (open each chat)
    logger.info(f"Bot {bot_profile_id}: Extracting messages from conversations...")
    notify_status({"status": "syncing", "message": "Extracting message history..."})

    # Limit to first 20 conversations to avoid taking too long
    conversations_to_sync = sidebar_data[:20]

    for idx, conv_info in enumerate(conversations_to_sync):
        chat_name = conv_info.get('name')
        if not chat_name:
            continue

        try:
            notify_status({
                "status": "syncing",
                "message": f"Reading messages from {chat_name[:20]}... ({idx + 1}/{len(conversations_to_sync)})"
            })

            # Click on the chat to open it
            title_selector = f'[title="{chat_name}"]'
            title_el = page.query_selector(title_selector)

            if not title_el:
                logger.warning(f"Bot {bot_profile_id}: Could not find chat: {chat_name}")
                continue

            title_el.click()
            time.sleep(2)  # Wait for chat to load

            # Verify chat opened
            conv_panel = page.query_selector('[data-testid="conversation-panel-messages"]') or \
                        page.query_selector('#main')

            if not conv_panel:
                logger.warning(f"Bot {bot_profile_id}: Chat did not open: {chat_name}")
                continue

            # Scroll up to load historical messages until we have at least max_history
            max_scroll_attempts = 15  # Safety limit for batch sync
            prev_message_count = 0

            # Use JavaScript to scroll to top first
            try:
                page.evaluate('''() => {
                    const panel = document.querySelector('[data-testid="conversation-panel-messages"]') ||
                                 document.querySelector('#main .copyable-area') ||
                                 document.querySelector('#main');
                    if (panel) panel.scrollTop = 0;
                }''')
                time.sleep(0.5)
            except Exception:
                pass

            for scroll_attempt in range(max_scroll_attempts):
                # Count current messages
                current_count = page.evaluate('''() => {
                    const msgs = document.querySelectorAll('[data-testid="msg-container"], .message-in, .message-out');
                    let count = 0;
                    for (const msg of msgs) {
                        const text = msg.querySelector('span.selectable-text, .copyable-text, [data-testid="msg-text"]');
                        if (text && text.innerText && text.innerText.trim().length > 0) count++;
                    }
                    return count;
                }''')

                # Stop if we have enough messages
                if current_count >= max_history:
                    break

                # Stop if no new messages were loaded (reached top of chat)
                if current_count == prev_message_count and scroll_attempt > 0:
                    break

                prev_message_count = current_count

                # Use keyboard navigation to scroll up - more reliable than scrollTop
                try:
                    for _ in range(3):
                        page.keyboard.press('PageUp')
                        time.sleep(0.2)
                except Exception:
                    # Fallback to JS scroll
                    page.evaluate('''() => {
                        const panel = document.querySelector('[data-testid="conversation-panel-messages"]') ||
                                     document.querySelector('#main');
                        if (panel) panel.scrollTop = 0;
                    }''')

                time.sleep(1.0)

            # Extract all messages from the conversation
            messages_data = page.evaluate('''() => {
                const messages = [];

                // Track current date from date separators
                let currentDateStr = null;

                // Get main panel to query all elements in order
                const mainPanel = document.querySelector('#main [role="application"]') ||
                                 document.querySelector('#main .copyable-area') ||
                                 document.querySelector('#main');

                if (!mainPanel) return messages;

                // Select all rows including date separators and messages
                const allRows = mainPanel.querySelectorAll('[role="row"], .focusable-list-item');

                for (const row of allRows) {
                    // Check if this is a date separator
                    const isMsgContainer = row.querySelector('[data-testid="msg-container"]') ||
                                          row.classList.contains('message-in') ||
                                          row.classList.contains('message-out') ||
                                          row.querySelector('.message-in, .message-out');

                    if (!isMsgContainer) {
                        // Extract date from separator
                        const dateSpan = row.querySelector('span[dir="auto"]');
                        if (dateSpan) {
                            const dateText = (dateSpan.innerText || '').trim();
                            if (/^\\d{1,2}\\/\\d{1,2}\\/\\d{2,4}$/.test(dateText)) {
                                currentDateStr = dateText;
                            } else if (/^(Today|Yesterday)$/i.test(dateText)) {
                                const today = new Date();
                                if (dateText.toLowerCase() === 'yesterday') {
                                    today.setDate(today.getDate() - 1);
                                }
                                currentDateStr = (today.getMonth() + 1) + '/' + today.getDate() + '/' + today.getFullYear();
                            }
                        }
                        continue;
                    }

                    // Find the actual message container
                    const msg = row.querySelector('[data-testid="msg-container"]') ||
                               row.querySelector('.message-in, .message-out') ||
                               (row.classList.contains('message-in') || row.classList.contains('message-out') ? row : null);

                    if (!msg) continue;
                    // Check if this is a media message (has image/document/video)
                    const hasMedia = msg.querySelector('[data-testid="image-thumb"]') ||
                                    msg.querySelector('[data-testid="video-thumb"]') ||
                                    msg.querySelector('[data-testid="document-thumb"]') ||
                                    msg.querySelector('[data-testid="audio-play"]') ||
                                    msg.querySelector('img[src*="blob:"]');

                    // Check if this is a group message
                    const dataIdAttr = msg.getAttribute('data-id') || '';
                    const isGroupMsg = dataIdAttr.includes('@g.us');

                    // Get message text
                    let text = '';
                    const selectableText = msg.querySelector('span.selectable-text');
                    if (selectableText) {
                        text = selectableText.innerText || '';
                    }
                    if (!text) {
                        const copyableText = msg.querySelector('.copyable-text');
                        if (copyableText) {
                            text = copyableText.innerText || '';
                        }
                    }
                    if (!text) {
                        const msgText = msg.querySelector('[data-testid="msg-text"]');
                        if (msgText) {
                            text = msgText.innerText || '';
                        }
                    }

                    text = text.trim();

                    // For media messages in groups, clean out sender name, phone, timestamp
                    if (hasMedia && text && isGroupMsg) {
                        const lines = text.split('\\n').map(l => l.trim()).filter(l => l);
                        const cleanLines = [];

                        // Get sender name to exclude it from content
                        let detectedSender = null;
                        const authorEl = msg.querySelector('[data-testid="author"]') ||
                                        msg.querySelector('[data-testid="msg-author-title"]') ||
                                        msg.querySelector('[data-testid="author-name"]');
                        if (authorEl) {
                            detectedSender = (authorEl.innerText || '').trim().toLowerCase();
                        }

                        for (const line of lines) {
                            const lineLower = line.toLowerCase();
                            // Skip if line matches sender name
                            if (detectedSender && lineLower === detectedSender) continue;
                            // Skip if line matches timestamp pattern (e.g., "8:21 AM", "14:30", "8:21 ص", "8:21 م")
                            if (/^\\d{1,2}:\\d{2}(\\s*([APap][Mm]|ص|م))?$/i.test(line)) continue;
                            // Skip if line matches phone number pattern (e.g., "+60 16-260 9676")
                            if (/^\\+?\\d[\\d\\s\\-()]{6,}$/.test(line)) continue;
                            // Skip common metadata patterns
                            if (/^(Yesterday|Today|\\d{1,2}\\/\\d{1,2}\\/\\d{2,4})$/i.test(line)) continue;
                            cleanLines.push(line);
                        }
                        text = cleanLines.join('\\n').trim();
                    }

                    if (!text || text.length < 1) continue;

                    // Check if outgoing (sent by us)
                    const isOutgoing = msg.classList.contains('message-out') ||
                                      msg.closest('.message-out') !== null ||
                                      msg.querySelector('[data-testid="msg-dblcheck"]') !== null ||
                                      msg.querySelector('[data-testid="msg-check"]') !== null;

                    // Get sender name (for group chats) - try multiple selectors
                    let sender = null;
                    const authorEl = msg.querySelector('[data-testid="author"]') ||
                                    msg.querySelector('[data-testid="msg-author-title"]') ||
                                    msg.querySelector('[data-testid="author-name"]') ||
                                    msg.querySelector('span[dir="auto"][aria-label]');
                    if (authorEl) {
                        sender = authorEl.innerText || authorEl.getAttribute('aria-label') || '';
                    }
                    // Fallback: extract from data-pre-plain-text attribute
                    if (!sender) {
                        const copyableText = msg.querySelector('.copyable-text[data-pre-plain-text]');
                        if (copyableText) {
                            const preText = copyableText.getAttribute('data-pre-plain-text') || '';
                            const match = preText.match(/\\]\\s*([^:]+):/);
                            if (match) {
                                sender = match[1].trim();
                            }
                        }
                    }

                    // Get sender profile picture (for group chats)
                    let senderProfilePic = null;

                    // In WhatsApp Web group chats, avatar is in a sibling/adjacent element
                    const msgRow = msg.closest('[data-testid="msg-container"]') ||
                                  msg.closest('[role="row"]') ||
                                  msg.closest('.message-in') ||
                                  msg.parentElement;

                    if (msgRow && !isOutgoing) {
                        // Try to find avatar in the message row or its siblings
                        const avatarSelectors = [
                            'img[data-testid="author-avatar"]',
                            '[data-testid="contact-avatar"] img',
                            '[data-testid="user-avatar"] img',
                            'div[role="button"] img[src*="pps.whatsapp.net"]',
                            'div[role="button"] img[src*="blob:"]',
                            'img[src*="pps.whatsapp.net"]',
                            'img[src*="web.whatsapp.com/pp"]',
                            'img[draggable="false"]'
                        ];

                        // Check within message row
                        for (const selector of avatarSelectors) {
                            const avatar = msgRow.querySelector(selector);
                            if (avatar && avatar.src) {
                                const src = avatar.src;
                                if (src.includes('pps.whatsapp.net') ||
                                    src.includes('web.whatsapp.com/pp') ||
                                    (src.includes('blob:') && avatar.width < 100 && avatar.width > 20)) {
                                    senderProfilePic = src;
                                    break;
                                }
                            }
                        }

                        // Check previous sibling
                        if (!senderProfilePic && msgRow.previousElementSibling) {
                            const avatar = msgRow.previousElementSibling.querySelector('img[src*="pps.whatsapp.net"]') ||
                                          msgRow.previousElementSibling.querySelector('img[src*="blob:"]');
                            if (avatar && avatar.src) {
                                senderProfilePic = avatar.src;
                            }
                        }

                        // Check parent container
                        if (!senderProfilePic && msgRow.parentElement) {
                            const avatars = msgRow.parentElement.querySelectorAll('img');
                            for (const avatar of avatars) {
                                if (avatar.src &&
                                    (avatar.src.includes('pps.whatsapp.net') || avatar.src.includes('blob:')) &&
                                    avatar.width > 20 && avatar.width < 60) {
                                    senderProfilePic = avatar.src;
                                    break;
                                }
                            }
                        }
                    }

                    // Get full timestamp from data-pre-plain-text attribute
                    // ONLY use timestamps that include both date and time - skip messages without valid timestamps
                    let fullTimestamp = null;
                    const copyableText = msg.querySelector('.copyable-text[data-pre-plain-text]');
                    if (copyableText) {
                        const preText = copyableText.getAttribute('data-pre-plain-text') || '';
                        // Extract full timestamp with date - this is the ONLY reliable source
                        const tsMatch = preText.match(/\\[(\\d{1,2}:\\d{2}(?:\\s*(?:[AP]M|ص|م))?),\\s*(\\d{1,2}\\/\\d{1,2}\\/\\d{2,4})\\]/i);
                        if (tsMatch) {
                            let timeStr = tsMatch[1].trim();
                            timeStr = timeStr.replace(/\\s*ص$/, ' AM').replace(/\\s*م$/, ' PM');
                            fullTimestamp = timeStr + ', ' + tsMatch[2].trim();
                        }
                    }

                    // Skip messages without valid full timestamp (date + time)
                    if (!fullTimestamp) {
                        continue;
                    }

                    messages.push({
                        text: text.substring(0, 2000),
                        isOutgoing: isOutgoing,
                        sender: sender,
                        senderProfilePic: senderProfilePic,
                        fullTimestamp: fullTimestamp,
                        messageIndex: messages.length,
                    });
                }

                return messages;
            }''')

            if messages_data:
                logger.info(f"Bot {bot_profile_id}: Found {len(messages_data)} messages in {chat_name}")

                # Store messages in database - use full data-id
                chat_data_id = conv_info.get('fullDataId')
                chat_id = chat_data_id if chat_data_id else chat_name.replace(" ", "_").replace("+", "")

                with get_db_session() as db:
                    conversation = db.query(Conversation).filter(
                        Conversation.bot_profile_id == bot_profile_id,
                        Conversation.chat_id == chat_id
                    ).first()

                    if not conversation:
                        conversation = db.query(Conversation).filter(
                            Conversation.bot_profile_id == bot_profile_id,
                            Conversation.chat_name == chat_name
                        ).first()

                    if conversation:
                        # Get existing messages to avoid duplicates
                        existing_messages = db.query(Message).filter(
                            Message.conversation_id == conversation.id
                        ).all()

                        # Create a set of existing message content for deduplication
                        existing_content = set()
                        for em in existing_messages:
                            # Use content + role as key for deduplication
                            key = f"{em.role}:{em.content[:100]}"
                            existing_content.add(key)

                        # Find the earliest existing message timestamp
                        earliest_existing = None
                        if existing_messages:
                            earliest_existing = min(m.timestamp for m in existing_messages if m.timestamp)

                        # Find the last assistant response index - messages after this are "new incoming"
                        last_responded_user_idx = -1
                        for idx in range(len(messages_data) - 1, -1, -1):
                            if messages_data[idx].get('isOutgoing', False):
                                last_responded_user_idx = idx
                                break

                        # Cache for sender profile pics
                        sender_profile_pics = {}

                        # Sort messages by messageIndex to ensure correct chronological order
                        messages_data.sort(key=lambda m: m.get('messageIndex', 0))

                        added_count = 0
                        for idx, msg in enumerate(messages_data):
                            role = "assistant" if msg.get('isOutgoing') else "user"
                            content = msg.get('text', '')

                            # Check for duplicate
                            key = f"{role}:{content[:100]}"
                            if key in existing_content:
                                continue  # Skip duplicate

                            # Skip user messages after the last assistant response (new incoming)
                            if role == "user" and idx > last_responded_user_idx and last_responded_user_idx >= 0:
                                continue

                            # Parse actual timestamp from WhatsApp (fullTimestamp has "HH:MM, DD/MM/YYYY")
                            # Use timezone_offset to convert to UTC if available
                            msg_timestamp = _parse_whatsapp_timestamp(
                                msg.get('fullTimestamp'),
                                msg.get('timestamp'),  # Fallback to just time
                                timezone_offset  # Convert to UTC using detected offset
                            )

                            # If no valid timestamp, use fallback with order-based adjustment
                            if not msg_timestamp:
                                msg_timestamp = datetime.utcnow() - timedelta(minutes=len(messages_data) - idx)

                            # If this is a historical message, ensure it's before existing new messages
                            if msg_timestamp and earliest_existing and msg_timestamp >= earliest_existing:
                                # Adjust to be before existing messages
                                msg_timestamp = earliest_existing - timedelta(minutes=len(messages_data) - idx)

                            # Get sender info
                            sender_pic = msg.get('senderProfilePic')
                            sender_name = msg.get('sender')
                            sender_id = _normalize_sender_id(msg.get('senderWhatsappId'))

                            if role == 'user':
                                # Set sender_name for private chats
                                if not sender_name and not is_group:
                                    sender_name = chat_name

                                # For private chats, get sender_id from conversation's chat_id
                                if not sender_id and not is_group and conversation.chat_id:
                                    sender_id = _normalize_sender_id(conversation.chat_id)

                                # Profile pic fallback logic
                                if not sender_pic:
                                    # Check cache first
                                    cache_key = sender_name or chat_name
                                    if cache_key in sender_profile_pics:
                                        sender_pic = sender_profile_pics[cache_key]
                                    elif not is_group:
                                        # For private chats, use conversation profile pic
                                        sender_pic = conversation.profile_pic
                                    else:
                                        # For groups, look up sender's private conversation
                                        sender_private_conv = db.query(Conversation).filter(
                                            Conversation.bot_profile_id == bot_profile_id,
                                            Conversation.is_group == False,
                                            Conversation.chat_name == sender_name
                                        ).first()
                                        if sender_private_conv and sender_private_conv.profile_pic:
                                            sender_pic = sender_private_conv.profile_pic
                                        sender_profile_pics[cache_key] = sender_pic
                                else:
                                    # Cache the extracted pic for future use
                                    cache_key = sender_name or chat_name
                                    sender_profile_pics[cache_key] = sender_pic
                            else:
                                # For assistant messages, set sender_name and profile pic
                                sender_name = "AI Agent"
                                sender_pic = AI_AGENT_PROFILE_PIC

                            new_msg = Message(
                                conversation_id=conversation.id,
                                role=role,
                                content=content,
                                sender_name=sender_name,
                                sender_id=sender_id,
                                sender_profile_pic=sender_pic,
                                timestamp=msg_timestamp
                            )
                            db.add(new_msg)
                            existing_content.add(key)  # Mark as added
                            added_count += 1

                        if added_count > 0:
                            db.commit()
                            total_messages += added_count
                            logger.info(f"Bot {bot_profile_id}: Added {added_count} new messages for {chat_name} (skipped {len(messages_data) - added_count} duplicates)")

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Error syncing chat {chat_name}: {e}")
            continue

    logger.info(f"Bot {bot_profile_id}: Full sync complete - {synced_count} conversations, {total_messages} messages")
    notify_status({"status": "running", "message": f"Synced {synced_count} conversations with {total_messages} messages"})

    return synced_count, total_messages


def _run_full_history_sync(page, instance, bot_profile_id, notify_status):
    """
    Run a full history sync for ALL conversations.
    Takes exclusive control of the browser page while running.
    Syncs up to instance.history_sync_count messages per conversation.
    Tracks progress via instance.history_sync_progress for frontend polling.
    """
    from app.database import get_db_session, Conversation, Message, BotProfile
    from datetime import datetime, timedelta

    target_count = instance.history_sync_count
    instance.history_sync_active = True
    instance.history_sync_requested = False
    instance.history_sync_progress = {
        "total": 0,
        "completed": 0,
        "current_chat": "",
        "status": "running"
    }
    notify_status({
        "status": "running",
        "message": "History sync started...",
        "history_sync_active": True,
        "history_sync_progress": instance.history_sync_progress,
    })

    logger.info(f"Bot {bot_profile_id}: Starting full history sync (target: {target_count} msgs/conversation)")

    # Step 1: Scan ALL sidebar conversations (not just unread) and upsert into DB
    notify_status({
        "status": "running",
        "message": "Scanning WhatsApp sidebar for conversations...",
        "history_sync_active": True,
        "history_sync_progress": instance.history_sync_progress,
    })

    # Close any open chat so sidebar is visible
    try:
        page.keyboard.press('Escape')
        time.sleep(0.5)
    except Exception:
        pass

    # Scroll sidebar to load all conversations (increased iterations for large contact lists)
    try:
        prev_height = 0
        for scroll_i in range(25):  # Increased from 10 to 25 for larger contact lists
            page.evaluate('''() => {
                const sidePanel = document.querySelector('#pane-side');
                if (sidePanel) sidePanel.scrollTop = sidePanel.scrollHeight;
            }''')
            time.sleep(0.4)
            # Check if we've reached the bottom
            current_height = page.evaluate('''() => {
                const sidePanel = document.querySelector('#pane-side');
                return sidePanel ? sidePanel.scrollHeight : 0;
            }''')
            if current_height == prev_height and scroll_i > 5:
                logger.info(f"Bot {bot_profile_id}: Sidebar fully loaded after {scroll_i + 1} scrolls")
                break
            prev_height = current_height
        # Scroll back to top
        page.evaluate('''() => {
            const sidePanel = document.querySelector('#pane-side');
            if (sidePanel) sidePanel.scrollTop = 0;
        }''')
        time.sleep(0.5)
    except Exception as e:
        logger.debug(f"Bot {bot_profile_id}: Sidebar scroll error: {e}")

    # Brief diagnostic
    try:
        diag = page.evaluate('''() => {
            const rows = document.querySelectorAll('#pane-side [role="row"]');
            const listitems = document.querySelectorAll('#pane-side [role="listitem"]');
            return { rowCount: rows.length, listitemCount: listitems.length };
        }''')
        print(f"Bot {bot_profile_id}: SIDEBAR rows={diag.get('rowCount')}, listitems={diag.get('listitemCount')}", flush=True)
    except Exception as diag_err:
        print(f"Bot {bot_profile_id}: SIDEBAR DIAGNOSTIC ERROR: {diag_err}", flush=True)

    # Extract ALL conversations from sidebar (no unread filter)
    # Uses broad selectors compatible with both WhatsApp Personal and Business
    sidebar_data = page.evaluate('''async () => {
        const conversations = [];
        const processedNames = new Set();
        await new Promise(r => setTimeout(r, 500));

        // Broad selector set: listitem (Personal), row (Business), cell-frame-container (fallback)
        const chatRows = document.querySelectorAll(
            '#pane-side [role="listitem"], #pane-side [role="row"], [data-testid="cell-frame-container"]'
        );

        for (const row of chatRows) {
            // Try multiple name selectors (WhatsApp Business uses different class names)
            let nameSpan = row.querySelector('span[title][dir="auto"]._ao3e') ||
                          row.querySelector('span._ao3e[title]') ||
                          row.querySelector('[data-testid="cell-frame-title"] span[title]') ||
                          row.querySelector('span.ggj6brxn[title]');

            // Fallback: find span[title] not in message preview area
            if (!nameSpan) {
                const allTitleSpans = row.querySelectorAll('span[title]');
                for (const span of allTitleSpans) {
                    const isInMessagePreview = span.closest('[data-testid="cell-frame-secondary"]') ||
                                              span.closest('[data-testid="last-msg-status"]') ||
                                              span.closest('[data-testid="msg-time"]');
                    if (isInMessagePreview) continue;
                    const title = span.getAttribute('title');
                    if (title && title.length <= 100) { nameSpan = span; break; }
                }
            }
            if (!nameSpan) continue;

            // Get name from both title attr and text content
            const titleAttr = nameSpan.getAttribute('title');
            const textContent = nameSpan.textContent ? nameSpan.textContent.trim() : null;

            // Determine which is the name (prefer non-phone-number)
            const isTextPhone = textContent && /^[\\+\\d][\\d\\s\\-\\(\\)]{7,}$/.test(textContent);
            const isTitlePhone = titleAttr && /^[\\+\\d][\\d\\s\\-\\(\\)]{7,}$/.test(titleAttr);

            let chatName;
            if (textContent && !isTextPhone) {
                chatName = textContent;
            } else if (titleAttr && !isTitlePhone) {
                chatName = titleAttr;
            } else {
                chatName = titleAttr || textContent;
            }

            if (!chatName || processedNames.has(chatName)) continue;
            processedNames.add(chatName);

            // Extract data-id with multiple methods (enhanced for WhatsApp Business)
            let fullDataId = null;

            // Method 1: Direct data-id on row
            let dataId = row.getAttribute('data-id');
            if (dataId && (dataId.includes('@c.us') || dataId.includes('@g.us'))) {
                fullDataId = dataId;
            }

            // Method 2: Immediate child with data-id
            if (!fullDataId) {
                const rowChild = row.querySelector(':scope > div[data-id]');
                if (rowChild) {
                    const id = rowChild.getAttribute('data-id');
                    if (id && (id.includes('@c.us') || id.includes('@g.us'))) fullDataId = id;
                }
            }

            // Method 3: Any child of row
            if (!fullDataId) {
                for (const child of row.children) {
                    const id = child.getAttribute('data-id');
                    if (id && (id.includes('@c.us') || id.includes('@g.us'))) { fullDataId = id; break; }
                }
            }

            // Method 4: Any descendant with data-id
            if (!fullDataId) {
                const dataIdElements = row.querySelectorAll('[data-id]');
                for (const elem of dataIdElements) {
                    const id = elem.getAttribute('data-id');
                    if (id && (id.includes('@c.us') || id.includes('@g.us'))) { fullDataId = id; break; }
                }
            }

            // Method 5: WhatsApp Business - check parent/ancestor for data-id
            if (!fullDataId) {
                let parent = row.parentElement;
                for (let i = 0; i < 3 && parent; i++) {
                    const id = parent.getAttribute('data-id');
                    if (id && (id.includes('@c.us') || id.includes('@g.us'))) { fullDataId = id; break; }
                    parent = parent.parentElement;
                }
            }

            // Method 6: Look for aria-selected row or focused chat link
            if (!fullDataId) {
                const chatLink = row.querySelector('a[href*="chat"]');
                if (chatLink) {
                    const href = chatLink.getAttribute('href');
                    const match = href && href.match(/chat\\/([^/]+@[cg]\\.us)/);
                    if (match) fullDataId = match[1];
                }
            }

            // Group detection: data-id suffix is source of truth
            let isGroup = false;
            if (fullDataId) {
                isGroup = fullDataId.includes('@g.us');
            } else {
                // Enhanced fallback for group detection (WhatsApp Business uses different icons)
                const avatarArea = row.querySelector('[data-testid="cell-frame-primary"]') || row;
                isGroup = avatarArea.querySelector('[data-icon="default-group"]') !== null ||
                         avatarArea.querySelector('[data-testid="default-group"]') !== null ||
                         avatarArea.querySelector('[data-icon="group"]') !== null ||
                         avatarArea.querySelector('span[data-icon*="group"]') !== null ||
                         // Check for multiple participants indicator (groups have multiple avatars or participant count)
                         row.querySelector('[data-testid="group-icon"]') !== null ||
                         // WhatsApp Business may use different attribute
                         row.querySelector('[aria-label*="group"]') !== null ||
                         row.querySelector('[aria-label*="Group"]') !== null;
            }

            conversations.push({
                name: chatName,
                fullDataId: fullDataId,
                isGroup: isGroup,
            });
        }
        return conversations;
    }''')

    # Count groups and contacts
    group_count = sum(1 for c in sidebar_data if c.get('isGroup'))
    contact_count = len(sidebar_data) - group_count
    print(f"Bot {bot_profile_id}: History sync - found {len(sidebar_data)} conversations ({group_count} groups, {contact_count} contacts)", flush=True)
    logger.info(f"Bot {bot_profile_id}: History sync - found {len(sidebar_data)} conversations ({group_count} groups, {contact_count} contacts)")

    # Debug: log first few discovered conversations
    for i, conv in enumerate(sidebar_data[:5]):
        print(f"Bot {bot_profile_id}: History sync sidebar[{i}]: name='{conv.get('name')}', dataId='{conv.get('fullDataId')}', isGroup={conv.get('isGroup')}", flush=True)

    # Log any groups found
    groups_found = [c for c in sidebar_data if c.get('isGroup')]
    if groups_found:
        for g in groups_found[:10]:  # Log up to 10 groups
            print(f"Bot {bot_profile_id}: History sync found group: '{g.get('name')}' (dataId={g.get('fullDataId')})", flush=True)
            logger.info(f"Bot {bot_profile_id}: History sync found group: '{g.get('name')}' (dataId={g.get('fullDataId')})")
    else:
        print(f"Bot {bot_profile_id}: History sync - WARNING: No groups detected in sidebar!", flush=True)
        logger.warning(f"Bot {bot_profile_id}: History sync - No groups detected in sidebar. Check if groups are visible or if selectors need updating.")

    if not sidebar_data:
        print(f"Bot {bot_profile_id}: History sync - NO conversations found in sidebar! Check WhatsApp selectors.", flush=True)

    # Upsert sidebar conversations into DB
    conversations_data = []
    with get_db_session() as db:
        for conv_info in sidebar_data:
            sidebar_name = conv_info.get('name')
            chat_data_id = conv_info.get('fullDataId')
            is_group = conv_info.get('isGroup', False)

            if not sidebar_name:
                continue

            chat_id = chat_data_id if _is_valid_whatsapp_id(chat_data_id) else None
            is_phone_format = bool(re.match(r'^[\+\d\s\-\(\)]+$', sidebar_name.strip()))
            display_name = sidebar_name if (not is_phone_format and not is_group) else None

            conversation = None
            if chat_id:
                conversation = db.query(Conversation).filter(
                    Conversation.bot_profile_id == bot_profile_id,
                    Conversation.chat_id == chat_id
                ).first()
            if not conversation:
                all_matching = db.query(Conversation).filter(
                    Conversation.bot_profile_id == bot_profile_id,
                    Conversation.chat_name == sidebar_name
                ).all()
                for existing in all_matching:
                    if _is_valid_whatsapp_id(existing.chat_id):
                        conversation = existing
                        break
                if not conversation and all_matching:
                    conversation = all_matching[0]

            if not conversation:
                # Use valid WhatsApp ID if available, otherwise use chat name as placeholder ID
                conv_chat_id = chat_data_id if _is_valid_whatsapp_id(chat_data_id) else sidebar_name
                conversation = Conversation(
                    bot_profile_id=bot_profile_id,
                    chat_id=conv_chat_id,
                    chat_name=sidebar_name,
                    display_name=display_name,
                    is_group=is_group,
                )
                db.add(conversation)
                db.flush()  # Get the ID
                print(f"Bot {bot_profile_id}: History sync - created conversation: {sidebar_name} (chat_id={conv_chat_id})", flush=True)
            else:
                if _is_valid_whatsapp_id(chat_data_id) and conversation.chat_id != chat_data_id:
                    conversation.chat_id = chat_data_id
                if display_name and not conversation.display_name:
                    conversation.display_name = display_name

            conversations_data.append({
                'id': conversation.id,
                'chat_id': conversation.chat_id,
                'chat_name': conversation.chat_name or sidebar_name,
                'is_group': conversation.is_group,
                'profile_pic': conversation.profile_pic,
            })

        db.commit()

    instance.history_sync_progress["total"] = len(conversations_data)
    notify_status({
        "status": "running",
        "message": f"Syncing history for {len(conversations_data)} conversations...",
        "history_sync_active": True,
        "history_sync_progress": instance.history_sync_progress,
    })

    logger.info(f"Bot {bot_profile_id}: History sync - {len(conversations_data)} conversations to process")

    synced_total = 0
    for conv_info in conversations_data:
        # Check for stop command (either bot stopping or user clicked stop sync)
        if not instance.is_running:
            logger.info(f"Bot {bot_profile_id}: History sync aborted - bot stopping")
            break
        if instance.history_sync_stop_requested:
            logger.info(f"Bot {bot_profile_id}: History sync stopped by user request")
            break

        chat_name = conv_info['chat_name']
        conv_id = conv_info['id']

        instance.history_sync_progress["current_chat"] = chat_name or ""
        notify_status({
            "status": "running",
            "message": f"Syncing: {chat_name}",
            "history_sync_active": True,
            "history_sync_progress": instance.history_sync_progress,
        })

        try:
            # Close any open chat first
            try:
                page.keyboard.press('Escape')
                time.sleep(0.3)
            except Exception:
                pass

            # Find the chat in the sidebar
            safe_name = (chat_name or "").replace('"', '\\"').replace("'", "\\'")
            chat_id = conv_info.get('chat_id', '')
            title_el = page.query_selector(f'#pane-side [title="{safe_name}"]')

            if not title_el:
                # Try partial match
                title_el = page.query_selector(f'#pane-side span[title*="{safe_name[:20]}"]')

            # Fallback: use search box to find the chat
            if not title_el:
                try:
                    search_term = chat_name
                    if chat_id and '@' in chat_id:
                        phone_part = chat_id.split('@')[0]
                        if phone_part.isdigit():
                            search_term = phone_part

                    search_box = page.query_selector('[data-testid="chat-list-search"]') or \
                                 page.query_selector('[contenteditable="true"][data-tab="3"]') or \
                                 page.query_selector('[aria-label*="Search"]')

                    if search_box:
                        search_box.click()
                        time.sleep(0.3)
                        search_box.fill(search_term)
                        time.sleep(1.5)

                        title_el = page.query_selector(f'[title="{safe_name}"]') or \
                                   page.query_selector(f'span[title*="{safe_name[:20]}"]') or \
                                   page.query_selector(f'span[title*="{search_term}"]')

                        if title_el:
                            logger.info(f"Bot {bot_profile_id}: History sync - found '{chat_name}' via search")
                except Exception as search_err:
                    logger.debug(f"Bot {bot_profile_id}: History sync - search failed for '{chat_name}': {search_err}")

            if not title_el:
                print(f"Bot {bot_profile_id}: History sync - chat not found: {chat_name}", flush=True)
                instance.history_sync_progress["completed"] += 1
                continue

            # Click to open the chat
            print(f"Bot {bot_profile_id}: History sync - opening chat: {chat_name}", flush=True)
            title_el.click()
            time.sleep(2.5)

            # Verify chat opened - wait for conversation panel
            conv_panel = None
            for _wait in range(5):
                conv_panel = page.query_selector('[data-testid="conversation-panel-messages"]') or \
                            page.query_selector('#main')
                if conv_panel:
                    break
                time.sleep(0.5)

            if not conv_panel:
                print(f"Bot {bot_profile_id}: History sync - chat did not open: {chat_name}", flush=True)
                instance.history_sync_progress["completed"] += 1
                continue

            # Wait for at least one message to render
            for _wait in range(6):
                has_msgs = page.evaluate('''() => {
                    return document.querySelectorAll('[data-testid="msg-container"], .message-in, .message-out').length;
                }''')
                if has_msgs and has_msgs > 0:
                    break
                time.sleep(0.5)

            # Extract profile pic and phone number from chat header + message data-id
            try:
                header_info = page.evaluate('''() => {
                    const result = {profilePic: null, phoneNumber: null, groupId: null, debug: {}};

                    // === PROFILE PIC: search broadly in the header area ===
                    // First try: any img with pps.whatsapp.net in the header section
                    const headerArea = document.querySelector('#main header') ||
                                      document.querySelector('[data-testid="conversation-header"]') ||
                                      document.querySelector('#main [data-testid="chat-header"]');

                    result.debug.headerFound = !!headerArea;

                    if (headerArea) {
                        // Get ALL images in the header
                        const headerImgs = headerArea.querySelectorAll('img');
                        result.debug.headerImgCount = headerImgs.length;
                        for (const img of headerImgs) {
                            if (img.src && img.src.includes('pps.whatsapp.net') &&
                                !img.src.includes('default-user') && !img.src.includes('default-group')) {
                                result.profilePic = img.src;
                                break;
                            }
                        }
                        // Fallback: any non-default, non-data img in header
                        if (!result.profilePic) {
                            for (const img of headerImgs) {
                                if (img.src && !img.src.startsWith('data:') &&
                                    !img.src.includes('default-user') && !img.src.includes('default-group') &&
                                    img.width >= 30 && img.height >= 30) {
                                    result.profilePic = img.src;
                                    break;
                                }
                            }
                        }
                    }

                    // Second try: search the avatar area before the header text
                    if (!result.profilePic) {
                        const avatarSelectors = [
                            '#main img[src*="pps.whatsapp.net"]',
                            '#main [data-testid*="avatar"] img',
                            '#main [role="img"] img',
                        ];
                        for (const selector of avatarSelectors) {
                            const img = document.querySelector(selector);
                            if (img && img.src && !img.src.includes('default-user') &&
                                !img.src.includes('default-group') && !img.src.startsWith('data:')) {
                                // Make sure this is in the header, not in messages
                                const msgPanel = document.querySelector('[data-testid="conversation-panel-messages"]');
                                if (msgPanel && msgPanel.contains(img)) continue;
                                result.profilePic = img.src;
                                break;
                            }
                        }
                    }

                    // === EXTRACT IDs FROM MESSAGE DATA-ID ATTRIBUTES ===
                    const dataIdEls = document.querySelectorAll('#main [data-id]');
                    result.debug.dataIdCount = dataIdEls.length;

                    for (const el of dataIdEls) {
                        const dataId = el.getAttribute('data-id') || '';

                        // GROUP chat: "false_GROUPID@g.us_MSGID_SENDERID@lid"
                        if (dataId.includes('@g.us') && !result.groupId) {
                            const parts = dataId.split('_');
                            for (let i = 1; i < parts.length; i++) {
                                if (parts[i].includes('@g.us')) {
                                    result.groupId = parts[i];  // Keep full format: "GROUPID@g.us"
                                    break;
                                }
                            }
                        }

                        // PRIVATE chat: "true_PHONE@c.us_MSGID" or "false_PHONE@c.us_MSGID"
                        if (dataId.includes('@c.us') && !result.phoneNumber && !dataId.includes('@g.us')) {
                            const parts = dataId.split('_');
                            for (let i = 1; i < parts.length; i++) {
                                if (parts[i].includes('@c.us')) {
                                    result.phoneNumber = '+' + parts[i].replace('@c.us', '');
                                    break;
                                }
                            }
                        }

                        // Stop if we found what we need
                        if (result.groupId || result.phoneNumber) break;
                    }

                    // === PHONE NUMBER from header (fallback for private chats) ===
                    if (!result.phoneNumber && !result.groupId && headerArea) {
                        const allSpans = headerArea.querySelectorAll('span[title], span[dir="auto"]');
                        for (const span of allSpans) {
                            const text = (span.getAttribute('title') || span.innerText || '').trim();
                            if (/^\\+?\\d[\\d\\s\\-()]{7,}$/.test(text)) {
                                result.phoneNumber = text.replace(/[\\s\\-()]/g, '');
                                break;
                            }
                        }
                    }

                    result.debug.profilePicFound = !!result.profilePic;
                    result.debug.phoneFound = !!result.phoneNumber;
                    result.debug.groupIdFound = !!result.groupId;
                    return result;
                }''')

                if header_info:
                    debug = header_info.get('debug', {})
                    print(f"Bot {bot_profile_id}: History sync - '{chat_name}' header: pic={debug.get('profilePicFound')}, phone={debug.get('phoneFound')}, groupId={debug.get('groupIdFound')}, headerFound={debug.get('headerFound')}, imgs={debug.get('headerImgCount', 0)}, dataIds={debug.get('dataIdCount', 0)}", flush=True)

                    with get_db_session() as db:
                        conv_to_update = db.query(Conversation).filter(Conversation.id == conv_id).first()
                        if conv_to_update:
                            updated = False
                            # Update profile pic if we got one and it's not already set
                            if header_info.get('profilePic') and not conv_to_update.profile_pic:
                                conv_to_update.profile_pic = header_info['profilePic']
                                updated = True

                            # For GROUP chats: update chat_id with group ID from message data-id
                            group_id = header_info.get('groupId')
                            if group_id and conv_info.get('is_group'):
                                # Group ID is already in format "GROUPID@g.us"
                                if conv_to_update.chat_id != group_id:
                                    conv_to_update.chat_id = group_id
                                    conv_to_update.is_group = True  # Ensure it's marked as group
                                    updated = True
                                    print(f"Bot {bot_profile_id}: History sync - '{chat_name}' updated group chat_id to {group_id}", flush=True)

                            # For PRIVATE chats: update chat_id with phone if current is a placeholder
                            # ONLY for non-group chats
                            phone = header_info.get('phoneNumber')
                            if phone and not conv_info.get('is_group') and not group_id and '@' not in (conv_to_update.chat_id or ''):
                                # Current chat_id is a placeholder (sidebar name), replace with phone
                                phone_clean = phone.lstrip('+')
                                conv_to_update.chat_id = f"{phone_clean}@c.us"
                                # Also store phone with + prefix for display
                                phone_formatted = phone if phone.startswith('+') else f"+{phone}"
                                conv_to_update.phone = phone_formatted
                                updated = True
                                print(f"Bot {bot_profile_id}: History sync - '{chat_name}' updated chat_id to {conv_to_update.chat_id}, phone to {phone_formatted}", flush=True)
                            if updated:
                                db.commit()
                                # Update local conv_info with new data
                                conv_info['profile_pic'] = conv_to_update.profile_pic
                                conv_info['chat_id'] = conv_to_update.chat_id
                                if phone:
                                    conv_info['phone'] = conv_to_update.phone
            except Exception as header_err:
                print(f"Bot {bot_profile_id}: History sync - header extraction error for '{chat_name}': {header_err}", flush=True)

            # For NON-GROUP chats: click header to open contact panel and extract phone number
            # This is necessary because data-id may contain LID instead of phone number
            if not conv_info.get('is_group'):
                try:
                    # Check if we need to extract phone (no valid phone in chat_id yet)
                    current_chat_id = conv_info.get('chat_id', '')
                    needs_phone = not current_chat_id or '@' not in current_chat_id or current_chat_id.endswith('@lid')

                    if needs_phone:
                        print(f"Bot {bot_profile_id}: History sync - '{chat_name}' clicking header to extract phone...", flush=True)

                        # Click the header to open contact details panel
                        # Try multiple selectors for both Business and Personal WhatsApp
                        header_clicked = False
                        header_selectors = [
                            '#main header [role="button"]',
                            '#main [data-tab="6"][role="button"]',
                            '#main header',
                        ]
                        for selector in header_selectors:
                            try:
                                header_btn = page.query_selector(selector)
                                if header_btn:
                                    header_btn.click()
                                    header_clicked = True
                                    break
                            except Exception:
                                continue

                        if header_clicked:
                            time.sleep(1.5)  # Wait for contact panel to open

                            # Extract phone number from contact details panel
                            # Display name is already extracted from sidebar - only need phone here
                            phone_from_panel = page.evaluate('''() => {
                                // Try multiple selectors for the phone number in contact panel
                                const phoneSelectors = [
                                    '.x1evy7pa.x1anpbxc span',
                                    '[data-testid="contact-info-phone"] span',
                                    'section span[dir="auto"]',
                                ];

                                for (const selector of phoneSelectors) {
                                    const elements = document.querySelectorAll(selector);
                                    for (const el of elements) {
                                        const text = (el.textContent || '').trim();
                                        // Match phone number pattern: starts with + or digit, 7+ digits
                                        if (/^\\+?\\d[\\d\\s\\-()]{7,}$/.test(text)) {
                                            return text.replace(/[\\s\\-()]/g, '');
                                        }
                                    }
                                }

                                // Fallback: search all spans in the right panel for phone pattern
                                const rightPanel = document.querySelector('[data-testid="contact-info-drawer"]') ||
                                                  document.querySelector('aside') ||
                                                  document.querySelector('section[data-testid]');
                                if (rightPanel) {
                                    const spans = rightPanel.querySelectorAll('span');
                                    for (const span of spans) {
                                        const text = (span.textContent || '').trim();
                                        if (/^\\+?\\d[\\d\\s\\-()]{7,}$/.test(text)) {
                                            return text.replace(/[\\s\\-()]/g, '');
                                        }
                                    }
                                }

                                return null;
                            }''')

                            # Close the contact panel
                            try:
                                page.keyboard.press('Escape')
                                time.sleep(0.5)
                            except Exception:
                                pass

                            if phone_from_panel:
                                print(f"Bot {bot_profile_id}: History sync - '{chat_name}' extracted phone from panel: {phone_from_panel}", flush=True)
                                # Update the database with the phone number
                                with get_db_session() as db:
                                    conv_to_update = db.query(Conversation).filter(Conversation.id == conv_id).first()
                                    if conv_to_update:
                                        phone_clean = phone_from_panel.lstrip('+')
                                        conv_to_update.chat_id = f"{phone_clean}@c.us"
                                        conv_to_update.phone = phone_from_panel  # Store with original format (e.g., +1234567890)
                                        db.commit()
                                        conv_info['chat_id'] = conv_to_update.chat_id
                                        conv_info['phone'] = phone_from_panel
                                        print(f"Bot {bot_profile_id}: History sync - '{chat_name}' updated chat_id to {conv_to_update.chat_id}, phone to {phone_from_panel}", flush=True)
                            else:
                                print(f"Bot {bot_profile_id}: History sync - '{chat_name}' could not extract phone from panel", flush=True)
                        else:
                            print(f"Bot {bot_profile_id}: History sync - '{chat_name}' could not click header", flush=True)
                except Exception as panel_err:
                    print(f"Bot {bot_profile_id}: History sync - '{chat_name}' contact panel phone extraction error: {panel_err}", flush=True)
                    # Close any open panel
                    try:
                        page.keyboard.press('Escape')
                        time.sleep(0.3)
                    except Exception:
                        pass

            # In-chat group detection: check header for group indicators and data-id on messages
            try:
                in_chat_is_group = page.evaluate('''() => {
                    // Method 1: Check ALL elements with data-id for @g.us
                    const dataIdEls = document.querySelectorAll('#main [data-id]');
                    for (const el of dataIdEls) {
                        const dataId = el.getAttribute('data-id') || '';
                        if (dataId.includes('@g.us')) return true;
                    }
                    // Method 2: Check for group-specific header elements
                    const header = document.querySelector('#main header, [data-testid="conversation-header"]');
                    if (header) {
                        // Group chats show member count or "click here for group info"
                        const subtitle = header.querySelector('[data-testid="conversation-info-header-chat-subtitle"]') ||
                                        header.querySelector('span[title*="participant"]') ||
                                        header.querySelector('span[title*="members"]');
                        if (subtitle) return true;
                        // Check for default-group icon
                        if (header.querySelector('[data-icon="default-group"]') ||
                            header.querySelector('[data-testid="default-group"]')) return true;
                    }
                    return false;
                }''')
                if in_chat_is_group and not conv_info.get('is_group'):
                    # Update in DB
                    with get_db_session() as db:
                        conv_to_update = db.query(Conversation).filter(Conversation.id == conv_id).first()
                        if conv_to_update and not conv_to_update.is_group:
                            conv_to_update.is_group = True
                            db.commit()
                            print(f"Bot {bot_profile_id}: History sync - '{chat_name}' detected as GROUP from in-chat header", flush=True)
                    conv_info['is_group'] = True
            except Exception:
                pass

            # Scroll up to load messages
            # For sync_all (-1), use a large number to load everything
            if target_count == -1:
                max_scroll_attempts = 500  # Effectively unlimited for most chats
            else:
                max_scroll_attempts = max(20, target_count // 5)  # Scale scroll attempts with target
            prev_message_count = 0

            # Initial scroll to top
            try:
                page.evaluate('''() => {
                    const panel = document.querySelector('[data-testid="conversation-panel-messages"]') ||
                                 document.querySelector('#main .copyable-area') ||
                                 document.querySelector('#main');
                    if (panel) panel.scrollTop = 0;
                }''')
                time.sleep(0.5)
            except Exception:
                pass

            no_change_count = 0
            scroll_attempt = 0
            min_scroll_before_break = 3  # Require at least 3 scroll attempts before giving up

            for scroll_attempt in range(max_scroll_attempts):
                if not instance.is_running or instance.history_sync_stop_requested:
                    break

                # Count ALL message containers (including media-only) for scroll decisions
                current_count = page.evaluate('''() => {
                    return document.querySelectorAll('[data-testid="msg-container"], .message-in, .message-out').length;
                }''')

                # For sync_all (-1), don't break based on count - continue until no more messages load
                if target_count != -1 and current_count >= target_count:
                    break

                if current_count == prev_message_count:
                    no_change_count += 1
                    # Only break after minimum attempts AND consecutive no-change
                    if scroll_attempt >= min_scroll_before_break and no_change_count >= 2:
                        break
                else:
                    no_change_count = 0

                prev_message_count = current_count

                try:
                    for _ in range(3):
                        page.keyboard.press('PageUp')
                        time.sleep(0.3)
                except Exception:
                    page.evaluate('''() => {
                        const selectors = [
                            '[data-testid="conversation-panel-messages"]',
                            '#main [role="application"]',
                            '#main .copyable-area',
                            '#main'
                        ];
                        for (const selector of selectors) {
                            const panel = document.querySelector(selector);
                            if (panel) { panel.scrollTop = 0; break; }
                        }
                    }''')

                time.sleep(1.5)

            print(f"Bot {bot_profile_id}: History sync - '{chat_name}' scroll done: {prev_message_count} containers after {scroll_attempt + 1 if scroll_attempt < max_scroll_attempts else max_scroll_attempts} attempts", flush=True)

            # Extract messages - same comprehensive JS as _sync_conversation_history_on_demand
            messages_data = page.evaluate('''() => {
                const messages = [];

                // Track current date from date separators
                // Date separators are focusable-list-item divs that contain just a date like "1/24/2026"
                let currentDateStr = null;

                // Get all elements in order: both messages AND date separators
                // Date separator structure: div.focusable-list-item > div > span with date text
                const mainPanel = document.querySelector('#main [role="application"]') ||
                                 document.querySelector('#main .copyable-area') ||
                                 document.querySelector('#main');

                if (!mainPanel) return messages;

                // Select all rows including date separators and messages
                const allRows = mainPanel.querySelectorAll('[role="row"], .focusable-list-item');

                for (const row of allRows) {
                    // Check if this is a date separator (contains only a date, no message)
                    // Date separator: has focusable-list-item class but no msg-container inside
                    const isMsgContainer = row.querySelector('[data-testid="msg-container"]') ||
                                          row.classList.contains('message-in') ||
                                          row.classList.contains('message-out') ||
                                          row.querySelector('.message-in, .message-out');

                    if (!isMsgContainer) {
                        // This might be a date separator - extract date
                        const dateSpan = row.querySelector('span[dir="auto"]');
                        if (dateSpan) {
                            const dateText = (dateSpan.innerText || '').trim();
                            // Check if it matches date pattern: M/D/YYYY, DD/MM/YYYY, or text like "Today", "Yesterday"
                            if (/^\\d{1,2}\\/\\d{1,2}\\/\\d{2,4}$/.test(dateText)) {
                                currentDateStr = dateText;
                            } else if (/^(Today|Yesterday)$/i.test(dateText)) {
                                // Convert relative dates
                                const today = new Date();
                                if (dateText.toLowerCase() === 'yesterday') {
                                    today.setDate(today.getDate() - 1);
                                }
                                currentDateStr = (today.getMonth() + 1) + '/' + today.getDate() + '/' + today.getFullYear();
                            }
                        }
                        continue;  // Skip to next element
                    }

                    // Find the actual message container
                    const msg = row.querySelector('[data-testid="msg-container"]') ||
                               row.querySelector('.message-in, .message-out') ||
                               (row.classList.contains('message-in') || row.classList.contains('message-out') ? row : null);

                    if (!msg) continue;
                    let text = '';
                    let mediaType = '';

                    // Detect media types
                    const hasImage = msg.querySelector('[data-testid="image-thumb"]') || msg.querySelector('img[src*="blob:"]');
                    const hasVideo = msg.querySelector('[data-testid="video-thumb"]');
                    const hasDocument = msg.querySelector('[data-testid="document-thumb"]');
                    const hasAudio = msg.querySelector('[data-testid="audio-play"]') || msg.querySelector('[data-testid="ptt-duration"]');
                    const hasSticker = msg.querySelector('[data-testid="sticker"]') || msg.querySelector('img[data-testid="sticker"]');
                    const hasMedia = hasImage || hasVideo || hasDocument || hasAudio || hasSticker;

                    // Check if this is a group message via data-id
                    // data-id may be on msg itself, or on a parent element (.message-in/.message-out wrapper)
                    const dataIdEl = msg.closest('[data-id]') || msg;
                    const dataIdAttr = dataIdEl.getAttribute('data-id') || '';
                    const isGroupMsg = dataIdAttr.includes('@g.us');

                    // For media messages, try caption-specific selectors first
                    if (hasMedia) {
                        // Determine media type for placeholder
                        if (hasVideo) mediaType = '[Video]';
                        else if (hasDocument) mediaType = '[Document]';
                        else if (hasAudio) mediaType = '[Audio]';
                        else if (hasSticker) mediaType = '[Sticker]';
                        else if (hasImage) mediaType = '[Image]';

                        const captionSelectors = [
                            '[data-testid="media-caption"] span.selectable-text',
                            '[data-testid="media-caption"]',
                        ];
                        for (const selector of captionSelectors) {
                            const captionEl = msg.querySelector(selector);
                            if (captionEl) {
                                text = (captionEl.innerText || '').trim();
                                if (text) break;
                            }
                        }
                    }

                    // Fallback to standard text extraction
                    if (!text) {
                        const selectableText = msg.querySelector('span.selectable-text');
                        if (selectableText) text = selectableText.innerText || '';
                    }
                    if (!text) {
                        const copyableText = msg.querySelector('.copyable-text');
                        if (copyableText) text = copyableText.innerText || '';
                    }
                    if (!text) {
                        const msgText = msg.querySelector('[data-testid="msg-text"]');
                        if (msgText) text = msgText.innerText || '';
                    }

                    text = text.trim();

                    // For media messages in groups, clean out sender name, phone, timestamp
                    if (hasMedia && text && isGroupMsg) {
                        const lines = text.split('\\n').map(l => l.trim()).filter(l => l);
                        const cleanLines = [];

                        // Get sender name to exclude it from content
                        let detectedSender = null;
                        const authorEl = msg.querySelector('[data-testid="author"]') ||
                                        msg.querySelector('[data-testid="msg-author-title"]') ||
                                        msg.querySelector('[data-testid="author-name"]');
                        if (authorEl) {
                            detectedSender = (authorEl.innerText || '').trim().toLowerCase();
                        }

                        for (const line of lines) {
                            const lineLower = line.toLowerCase();
                            // Skip if line matches sender name
                            if (detectedSender && lineLower === detectedSender) continue;
                            // Skip if line matches timestamp pattern
                            if (/^\\d{1,2}:\\d{2}(\\s*([APap][Mm]|ص|م))?$/i.test(line)) continue;
                            // Skip if line matches phone number pattern
                            if (/^\\+?\\d[\\d\\s\\-()]{6,}$/.test(line)) continue;
                            // Skip common metadata patterns
                            if (/^(Yesterday|Today|\\d{1,2}\\/\\d{1,2}\\/\\d{2,4})$/i.test(line)) continue;
                            cleanLines.push(line);
                        }
                        text = cleanLines.join('\\n').trim();
                    }

                    // For media without text, use the media type as placeholder
                    if (!text && mediaType) {
                        text = mediaType;
                    }

                    // Skip system messages and empty non-media messages
                    if (!text || text.length < 1) continue;

                    const isOutgoing = msg.classList.contains('message-out') ||
                                      msg.closest('.message-out') !== null ||
                                      msg.querySelector('[data-testid="msg-dblcheck"]') !== null ||
                                      msg.querySelector('[data-testid="msg-check"]') !== null;

                    // Get sender name - try multiple selectors (5 methods for group chats)
                    let sender = null;
                    if (!isOutgoing) {
                        // Method 1: Standard author elements
                        const authorEl = msg.querySelector('[data-testid="author"]') ||
                                        msg.querySelector('[data-testid="msg-author-title"]') ||
                                        msg.querySelector('[data-testid="author-name"]') ||
                                        msg.querySelector('span[dir="auto"][aria-label]');
                        if (authorEl) {
                            sender = authorEl.innerText || authorEl.getAttribute('aria-label') || '';
                        }

                        // Method 2: Extract from data-pre-plain-text attribute
                        if (!sender) {
                            const copyableEl = msg.querySelector('.copyable-text[data-pre-plain-text]');
                            if (copyableEl) {
                                const preText = copyableEl.getAttribute('data-pre-plain-text') || '';
                                const match = preText.match(/\\]\\s*([^:]+):/);
                                if (match) {
                                    sender = match[1].trim();
                                }
                            }
                        }

                        // Method 3: For MEDIA messages in groups - colored span at top
                        if (!sender) {
                            const msgContainer = msg.closest('[data-testid="msg-container"]') || msg;
                            const allSpans = msgContainer.querySelectorAll('span[dir="auto"]');
                            for (const span of allSpans) {
                                const style = window.getComputedStyle(span);
                                const color = style.color;
                                const spanText = (span.innerText || '').trim();

                                if (spanText && spanText.length > 0 && spanText.length < 30) {
                                    if (color && !color.includes('rgb(255, 255, 255)') &&
                                        !color.includes('rgba(255, 255, 255') &&
                                        !color.includes('rgb(0, 0, 0)') &&
                                        !color.includes('rgba(0, 0, 0')) {
                                        const spanRect = span.getBoundingClientRect();
                                        const msgRect = msgContainer.getBoundingClientRect();
                                        if (spanRect.top - msgRect.top < 50) {
                                            if (!spanText.match(/^\\d+:\\d+/) &&
                                                !spanText.match(/^[\\d,.]+ [KMG]?B$/i) &&
                                                !spanText.match(/^\\d+ page/i) &&
                                                spanText !== 'PDF' && spanText !== 'DOC' && spanText !== 'XLS') {
                                                sender = spanText;
                                                break;
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // Method 4: Check for sender in message row's context
                        if (!sender) {
                            const msgRow = msg.closest('[role="row"]') || msg.parentElement;
                            if (msgRow) {
                                const rowAuthor = msgRow.querySelector('[data-testid="author"]') ||
                                                 msgRow.querySelector('[data-testid="msg-author-title"]');
                                if (rowAuthor) {
                                    sender = rowAuthor.innerText || '';
                                }
                            }
                        }

                        // Method 5: Check aria-label on the message container
                        if (!sender) {
                            const ariaLabel = msg.getAttribute('aria-label') || '';
                            const ariaMatch = ariaLabel.match(/from\\s+([^,:.]+)/i);
                            if (ariaMatch) {
                                sender = ariaMatch[1].trim();
                            }
                        }
                    }

                    // Get sender profile picture (for group chats)
                    // IMPORTANT: In Private WhatsApp, avatar is in a SIBLING div BEFORE the message row
                    // Structure: <div class="x1n2onr6"> contains:
                    //   - <div class="x16ye13r..."> with avatar button + img (SIBLING)
                    //   - <div role="row"> with message content (SIBLING)
                    let senderProfilePic = null;
                    let senderAvatarButtonSelector = null;  // For clicking to get contact info
                    let senderPhoneFromHeader = null;  // Phone visible in Business WhatsApp message header

                    if (!isOutgoing) {
                        // Find the outermost container that holds both avatar and message
                        const msgRow = msg.closest('[role="row"]');
                        const outerContainer = msgRow ? msgRow.parentElement : msg.closest('.x1n2onr6');

                        if (outerContainer) {
                            // Look for avatar in PREVIOUS SIBLING of message row
                            // The avatar is in: div.x16ye13r > div[role="button"][aria-label*="Open chat details"] > img
                            const avatarSibling = outerContainer.querySelector('.x16ye13r, [class*="x16ye13r"]');
                            if (avatarSibling) {
                                const avatarButton = avatarSibling.querySelector('div[role="button"][aria-label*="Open chat details"]');
                                if (avatarButton) {
                                    // Store selector for later clicking
                                    const ariaLabel = avatarButton.getAttribute('aria-label') || '';
                                    if (ariaLabel) {
                                        senderAvatarButtonSelector = `div[role="button"][aria-label="${ariaLabel.replace(/"/g, '\\"')}"]`;
                                    }

                                    // Get profile pic from the button
                                    const avatarImg = avatarButton.querySelector('img');
                                    if (avatarImg && avatarImg.src &&
                                        (avatarImg.src.includes('pps.whatsapp.net') || avatarImg.src.includes('media-')) &&
                                        !avatarImg.src.includes('default-user')) {
                                        senderProfilePic = avatarImg.src;
                                    }
                                }
                            }

                            // Fallback: search for any img with profile pic URL in outer container
                            if (!senderProfilePic) {
                                const fallbackImg = outerContainer.querySelector('img[src*="pps.whatsapp.net"]') ||
                                                   outerContainer.querySelector('img[src*="media-"][draggable="false"]');
                                if (fallbackImg && fallbackImg.src && !fallbackImg.src.includes('default-user')) {
                                    senderProfilePic = fallbackImg.src;
                                }
                            }
                        }

                        // Also try the previous patterns as fallback
                        if (!senderProfilePic && msgRow) {
                            const avatarSelectors = [
                                'img[data-testid="author-avatar"]',
                                '[data-testid="contact-avatar"] img',
                                'div[role="button"] img[src*="pps.whatsapp.net"]',
                                'img[src*="pps.whatsapp.net"]',
                            ];
                            for (const selector of avatarSelectors) {
                                const avatar = msgRow.querySelector(selector);
                                if (avatar && avatar.src && !avatar.src.includes('default-user')) {
                                    senderProfilePic = avatar.src;
                                    break;
                                }
                            }
                        }

                        // Extract phone from message header (Business WhatsApp shows phone in header)
                        // Look for: <span class="_ahx_" role="button">+60 16-260 9676</span>
                        const phoneSpan = msg.querySelector('span._ahx_[role="button"]') ||
                                         msg.querySelector('span[class*="_ahx"]');
                        if (phoneSpan) {
                            const phoneText = (phoneSpan.innerText || '').trim();
                            if (/^\\+?\\d[\\d\\s\\-()]{6,}$/.test(phoneText)) {
                                senderPhoneFromHeader = phoneText.replace(/[\\s\\-()]/g, '');
                            }
                        }
                    }

                    // Extract ALL components from data-id attribute
                    // Format: "{isOutgoing}_{groupId}@g.us_{messageId}_{senderId}@lid"
                    // Example: "false_120363425279786981@g.us_AC6D878D90A12D0C36C39AC9E709F070_232336423682301@lid"
                    let senderWhatsappId = null;
                    let groupId = null;
                    let messageWhatsappId = null;

                    if (dataIdAttr) {
                        const parts = dataIdAttr.split('_');
                        if (parts.length >= 2) {
                            if (isGroupMsg) {
                                // Group message format: {bool}_{groupId}@g.us_{msgId}_{senderId}@lid
                                for (let i = 1; i < parts.length; i++) {
                                    if (parts[i].includes('@g.us')) {
                                        groupId = parts[i];  // Keep full format: "120363425279786981@g.us"
                                    } else if (parts[i].includes('@lid')) {
                                        senderWhatsappId = parts[i].replace('@lid', '');
                                    } else if (!parts[i].includes('@') && parts[i].length > 10) {
                                        // This is likely the message ID (long alphanumeric string)
                                        messageWhatsappId = parts[i];
                                    }
                                }
                            } else {
                                // Private chat format: {bool}_{phone}@c.us_{msgId} or {bool}_{lid}@lid_{msgId}
                                for (let i = 1; i < parts.length; i++) {
                                    if (parts[i].includes('@c.us')) {
                                        senderWhatsappId = parts[i].replace('@c.us', '');
                                    } else if (parts[i].includes('@lid')) {
                                        senderWhatsappId = parts[i].replace('@lid', '');
                                    } else if (!parts[i].includes('@') && parts[i].length > 10) {
                                        messageWhatsappId = parts[i];
                                    }
                                }
                            }
                        }
                    }

                    // Get full timestamp from data-pre-plain-text attribute
                    // ONLY use timestamps that include both date and time - skip messages without valid timestamps
                    let fullTimestamp = null;
                    let preTextDebug = null;
                    const copyableTextTs = msg.querySelector('.copyable-text[data-pre-plain-text]');
                    if (copyableTextTs) {
                        const preText = copyableTextTs.getAttribute('data-pre-plain-text') || '';
                        preTextDebug = preText.substring(0, 100);
                        // Extract full timestamp with date - this is the ONLY reliable source
                        const tsMatch = preText.match(/\\[(\\d{1,2}:\\d{2}(?:\\s*(?:[AP]M|ص|م))?),\\s*(\\d{1,2}\\/\\d{1,2}\\/\\d{2,4})\\]/i);
                        if (tsMatch) {
                            let timeStr = tsMatch[1].trim();
                            timeStr = timeStr.replace(/\\s*ص$/, ' AM').replace(/\\s*م$/, ' PM');
                            fullTimestamp = timeStr + ', ' + tsMatch[2].trim();
                        }
                    }

                    // Skip messages without valid full timestamp (date + time)
                    // It's better to skip than to display with incorrect date
                    if (!fullTimestamp) {
                        continue;
                    }

                    messages.push({
                        text: text.substring(0, 2000),
                        isOutgoing: isOutgoing,
                        isGroupMsg: isGroupMsg,
                        sender: sender,
                        senderProfilePic: senderProfilePic,
                        senderWhatsappId: senderWhatsappId,
                        senderPhoneFromHeader: senderPhoneFromHeader,
                        senderAvatarButtonSelector: senderAvatarButtonSelector,
                        groupId: groupId,
                        messageWhatsappId: messageWhatsappId,
                        fullDataId: dataIdAttr,
                        fullTimestamp: fullTimestamp,
                        preTextDebug: preTextDebug,
                        messageIndex: messages.length,  // Preserve order within page
                    });
                }
                return messages;
            }''')

            # Store messages in DB with deduplication
            print(f"Bot {bot_profile_id}: History sync - '{chat_name}' extracted {len(messages_data) if messages_data else 0} messages from page", flush=True)
            added_count = 0
            if messages_data and len(messages_data) > 0:
                # Sort messages by messageIndex to ensure correct chronological order
                # messageIndex captures DOM order (top=oldest to bottom=newest)
                # This is critical when messages have the same timestamp (same minute)
                messages_data.sort(key=lambda m: m.get('messageIndex', 0))
                # Check if any message's data-id indicates this is a group
                # This is more reliable than sidebar-based group detection
                detected_as_group = any(m.get('isGroupMsg') for m in messages_data)

                # Extract group ID from messages (for groups)
                extracted_group_id = None
                for m in messages_data:
                    if m.get('groupId'):
                        extracted_group_id = m.get('groupId')
                        break

                # Get bot timezone offset
                timezone_offset = None
                with get_db_session() as db:
                    bot = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
                    if bot:
                        timezone_offset = bot.whatsapp_timezone_offset

                # === CLICK-TO-EXTRACT: Get phone numbers for unique senders in groups ===
                sender_info_cache = {}  # {senderId: {phone, profile_pic}}

                if detected_as_group:
                    # Collect unique senders who need phone extraction
                    unique_senders = {}  # {senderId: {name, avatarSelector, profilePic, phoneFromHeader}}
                    for m in messages_data:
                        if m.get('isOutgoing'):
                            continue
                        sender_id = m.get('senderWhatsappId')
                        if not sender_id or sender_id in unique_senders:
                            continue
                        unique_senders[sender_id] = {
                            'name': m.get('sender'),
                            'avatarSelector': m.get('senderAvatarButtonSelector'),
                            'profilePic': m.get('senderProfilePic'),
                            'phoneFromHeader': m.get('senderPhoneFromHeader'),
                        }

                    print(f"Bot {bot_profile_id}: History sync - '{chat_name}' found {len(unique_senders)} unique senders", flush=True)

                    # For each unique sender, try to get their phone number
                    for sender_id, sender_data in unique_senders.items():
                        # Already have phone from header (Business WhatsApp)?
                        if sender_data.get('phoneFromHeader'):
                            sender_info_cache[sender_id] = {
                                'phone': sender_data['phoneFromHeader'],
                                'profile_pic': sender_data.get('profilePic'),
                            }
                            print(f"Bot {bot_profile_id}: History sync - sender '{sender_data.get('name')}' phone from header: {sender_data['phoneFromHeader']}", flush=True)
                            continue

                        # Try to click avatar button to get contact info
                        avatar_selector = sender_data.get('avatarSelector')
                        if avatar_selector:
                            try:
                                avatar_btn = page.query_selector(avatar_selector)
                                if avatar_btn:
                                    avatar_btn.click()
                                    time.sleep(1.5)

                                    # Extract phone from contact panel
                                    contact_phone = page.evaluate('''() => {
                                        const phoneSelectors = [
                                            '.x1evy7pa.x1anpbxc span',
                                            '[data-testid="contact-info-phone"] span',
                                            'section span[dir="auto"]',
                                        ];
                                        for (const selector of phoneSelectors) {
                                            const elements = document.querySelectorAll(selector);
                                            for (const el of elements) {
                                                const text = (el.textContent || '').trim();
                                                if (/^\\+?\\d[\\d\\s\\-()]{7,}$/.test(text)) {
                                                    return text.replace(/[\\s\\-()]/g, '');
                                                }
                                            }
                                        }
                                        // Fallback: search all spans in right panel
                                        const panel = document.querySelector('[data-testid="contact-info-drawer"]') ||
                                                     document.querySelector('aside') ||
                                                     document.querySelector('section[data-testid]');
                                        if (panel) {
                                            const spans = panel.querySelectorAll('span');
                                            for (const span of spans) {
                                                const text = (span.textContent || '').trim();
                                                if (/^\\+?\\d[\\d\\s\\-()]{7,}$/.test(text)) {
                                                    return text.replace(/[\\s\\-()]/g, '');
                                                }
                                            }
                                        }
                                        return null;
                                    }''')

                                    # Close the panel
                                    page.keyboard.press('Escape')
                                    time.sleep(0.5)

                                    if contact_phone:
                                        sender_info_cache[sender_id] = {
                                            'phone': contact_phone,
                                            'profile_pic': sender_data.get('profilePic'),
                                        }
                                        print(f"Bot {bot_profile_id}: History sync - sender '{sender_data.get('name')}' phone from panel: {contact_phone}", flush=True)
                                    else:
                                        # Cache without phone but with profile pic
                                        sender_info_cache[sender_id] = {
                                            'phone': None,
                                            'profile_pic': sender_data.get('profilePic'),
                                        }
                            except Exception as click_err:
                                print(f"Bot {bot_profile_id}: History sync - error clicking sender avatar: {click_err}", flush=True)
                                try:
                                    page.keyboard.press('Escape')
                                    time.sleep(0.3)
                                except:
                                    pass
                        else:
                            # No avatar selector, just cache profile pic
                            sender_info_cache[sender_id] = {
                                'phone': None,
                                'profile_pic': sender_data.get('profilePic'),
                            }

                with get_db_session() as db:
                    conv_obj = db.query(Conversation).filter(Conversation.id == conv_id).first()
                    is_group_conv = conv_obj.is_group if conv_obj else False
                    conv_chat_name = conv_obj.chat_name if conv_obj else chat_name
                    conv_profile_pic = conv_obj.profile_pic if conv_obj else None

                    # Update group status if detected from message data-id
                    if detected_as_group and conv_obj and not conv_obj.is_group:
                        conv_obj.is_group = True
                        is_group_conv = True
                        print(f"Bot {bot_profile_id}: History sync - '{chat_name}' detected as GROUP from message data-id", flush=True)

                    # Update chat_id with GROUP ID if this is a group
                    if detected_as_group and extracted_group_id and conv_obj:
                        if conv_obj.chat_id != extracted_group_id:
                            conv_obj.chat_id = extracted_group_id
                            print(f"Bot {bot_profile_id}: History sync - '{chat_name}' updated group chat_id: {extracted_group_id}", flush=True)
                    # For private chats: update chat_id from message data-id if still a placeholder
                    elif not detected_as_group and conv_obj and '@' not in (conv_obj.chat_id or ''):
                        for m in messages_data:
                            wid = m.get('senderWhatsappId')
                            if wid and wid.strip():
                                phone_clean = wid.replace('+', '').replace(' ', '').replace('-', '')
                                if phone_clean.isdigit() and len(phone_clean) >= 7:
                                    conv_obj.chat_id = f"{phone_clean}@c.us"
                                    conv_obj.phone = f"+{phone_clean}"  # Store with + prefix for display
                                    print(f"Bot {bot_profile_id}: History sync - '{chat_name}' updated chat_id from message data-id: {conv_obj.chat_id}, phone: {conv_obj.phone}", flush=True)
                                    break

                    # Re-read conv fields after potential updates
                    conv_chat_name = conv_obj.chat_name if conv_obj else chat_name
                    conv_profile_pic = conv_obj.profile_pic if conv_obj else None

                    # Build set of existing message content for deduplication
                    existing_msgs = db.query(Message.content, Message.role).filter(
                        Message.conversation_id == conv_id
                    ).all()
                    existing_content = set()
                    for em in existing_msgs:
                        existing_content.add((em.content[:100] if em.content else "", em.role))

                    # Find earliest existing timestamp for ordering
                    earliest_existing = None
                    existing_timestamps = db.query(Message.timestamp).filter(
                        Message.conversation_id == conv_id,
                        Message.timestamp.isnot(None)
                    ).all()
                    if existing_timestamps:
                        earliest_existing = min(t[0] for t in existing_timestamps)

                    # Cache for sender profile pics from private conversations
                    sender_profile_pics = {}

                    # Find the last user message that has NO response after it
                    # These are "new incoming" messages that should NOT be stored in history
                    last_responded_user_idx = -1
                    for scan_idx in range(len(messages_data) - 1, -1, -1):
                        if messages_data[scan_idx].get('isOutgoing', False):
                            last_responded_user_idx = scan_idx
                            break

                    for idx, msg in enumerate(messages_data):
                        role = "assistant" if msg.get('isOutgoing') else "user"
                        content = msg.get('text', '')

                        # Simple deduplication: skip if same content+role already exists
                        key = (content[:100], role)
                        if key in existing_content:
                            continue

                        # Skip user messages after the last assistant response
                        # These are "new incoming" messages - let normal processing handle them
                        if role == "user" and idx > last_responded_user_idx and last_responded_user_idx >= 0:
                            continue

                        # Debug timestamp extraction
                        if msg.get('preTextDebug'):
                            logger.info(f"Bot {bot_profile_id}: HIST TIMESTAMP DEBUG - preText='{msg.get('preTextDebug')}', fullTimestamp='{msg.get('fullTimestamp')}'")

                        # Parse actual timestamp from WhatsApp
                        msg_timestamp = _parse_whatsapp_timestamp(msg.get('fullTimestamp'), None, timezone_offset)
                        if not msg_timestamp:
                            if earliest_existing:
                                msg_timestamp = earliest_existing - timedelta(minutes=len(messages_data) - idx)
                            else:
                                msg_timestamp = datetime.utcnow() - timedelta(minutes=len(messages_data) - idx)

                        # Get sender name, profile pic, WhatsApp ID, and phone
                        sender_name = msg.get('sender')
                        sender_pic = msg.get('senderProfilePic')
                        sender_whatsapp_id = _normalize_sender_id(msg.get('senderWhatsappId'))
                        sender_phone = msg.get('senderPhoneFromHeader')  # From Business WhatsApp header

                        if role == "user":
                            if is_group_conv:
                                # For group chats, use extracted sender name, pic, and phone
                                raw_sender_id = msg.get('senderWhatsappId')

                                # First, check sender_info_cache (from click-to-extract)
                                if raw_sender_id and raw_sender_id in sender_info_cache:
                                    cached_info = sender_info_cache[raw_sender_id]
                                    if not sender_pic and cached_info.get('profile_pic'):
                                        sender_pic = cached_info['profile_pic']
                                    if not sender_phone and cached_info.get('phone'):
                                        sender_phone = cached_info['phone']

                                if sender_name:
                                    # If no pic extracted from message or cache, check private conversation
                                    if not sender_pic:
                                        if sender_name in sender_profile_pics:
                                            sender_pic = sender_profile_pics[sender_name]
                                        else:
                                            # Look up sender's private conversation
                                            sender_private_conv = db.query(Conversation).filter(
                                                Conversation.bot_profile_id == bot_profile_id,
                                                Conversation.is_group == False,
                                                Conversation.chat_name == sender_name
                                            ).first()
                                            if sender_private_conv and sender_private_conv.profile_pic:
                                                sender_pic = sender_private_conv.profile_pic
                                            sender_profile_pics[sender_name] = sender_pic
                                    else:
                                        # Cache the extracted pic for future use
                                        sender_profile_pics[sender_name] = sender_pic
                            else:
                                # For private chats, sender is the contact (chat_name)
                                sender_name = conv_chat_name
                                sender_pic = conv_profile_pic
                                # For private chats, try to extract sender_id from conversation's chat_id
                                if not sender_whatsapp_id and conv_obj and conv_obj.chat_id:
                                    sender_whatsapp_id = _normalize_sender_id(conv_obj.chat_id)
                        else:
                            # For assistant messages, use bot's actual WhatsApp profile pic and phone
                            sender_name = "AI Agent"
                            bot_obj = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
                            if bot_obj:
                                if bot_obj.whatsapp_profile_pic:
                                    sender_pic = bot_obj.whatsapp_profile_pic
                                else:
                                    sender_pic = AI_AGENT_PROFILE_PIC
                                if bot_obj.whatsapp_phone:
                                    sender_whatsapp_id = bot_obj.whatsapp_phone.replace('+', '').replace(' ', '').replace('-', '')
                            else:
                                sender_pic = AI_AGENT_PROFILE_PIC

                        # Get WhatsApp message ID for tracking
                        whatsapp_msg_id = msg.get('messageWhatsappId')

                        # Clean up sender_phone for storage
                        sender_phone_clean = None
                        if sender_phone:
                            phone_clean = sender_phone.replace('+', '').replace(' ', '').replace('-', '')
                            if phone_clean.isdigit() and len(phone_clean) >= 7:
                                sender_phone_clean = phone_clean

                        new_msg = Message(
                            conversation_id=conv_id,
                            role=role,
                            content=content,
                            sender_name=sender_name,
                            sender_id=sender_whatsapp_id,
                            sender_phone=sender_phone_clean,
                            sender_profile_pic=sender_pic,
                            whatsapp_message_id=whatsapp_msg_id,
                            timestamp=msg_timestamp
                        )
                        db.add(new_msg)
                        existing_content.add(key)
                        added_count += 1

                    # Mark conversation as synced
                    if conv_obj:
                        conv_obj.history_synced = True
                        conv_obj.last_synced_at = datetime.utcnow()
                        conv_obj.message_count = (conv_obj.message_count or 0) + added_count

                    db.commit()

            synced_total += added_count
            print(f"Bot {bot_profile_id}: History sync - '{chat_name}' done, {added_count} messages added", flush=True)

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: History sync error for '{chat_name}': {e}", exc_info=True)

        instance.history_sync_progress["completed"] += 1

    # Close any open chat
    try:
        page.keyboard.press('Escape')
        time.sleep(0.3)
    except Exception:
        pass

    # Mark sync as complete and reset flags
    instance.history_sync_active = False
    instance.history_sync_stop_requested = False
    instance.history_sync_progress["status"] = "completed"
    instance.history_sync_progress["current_chat"] = ""
    notify_status({
        "status": "running",
        "message": f"History sync complete - {synced_total} messages synced",
        "history_sync_active": False,
        "history_sync_progress": instance.history_sync_progress,
    })
    logger.info(f"Bot {bot_profile_id}: Full history sync complete - {synced_total} total messages synced across {len(conversations_data)} conversations")


def _sync_conversation_history_on_demand(page, bot_profile_id, conversation_id, chat_name):
    """
    Sync message history for a SINGLE conversation on-demand.
    Called before generating AI response if history hasn't been synced yet.
    Scrolls up until we have at least max_history messages for AI context.
    Returns the number of messages synced.
    """
    from app.database import get_db_session, Conversation, Message, BotProfile
    from datetime import datetime, timedelta
    import time

    # Get bot's max_history setting and timezone offset
    max_history = 20  # Default
    timezone_offset = None  # Will use detected offset if available
    with get_db_session() as db:
        bot = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
        if bot:
            max_history = bot.max_history or 20
            timezone_offset = bot.whatsapp_timezone_offset  # May be None if not yet detected

    logger.info(f"Bot {bot_profile_id}: On-demand sync for conversation '{chat_name}' (ID: {conversation_id}, target: {max_history} messages, tz_offset: {timezone_offset})")

    try:
        # Check if chat is already open (we're called from message processing which already opened it)
        conv_panel = page.query_selector('[data-testid="conversation-panel-messages"]') or \
                    page.query_selector('#main')

        if not conv_panel:
            # Chat not open - need to click to open it
            # Search in sidebar specifically (not header)
            safe_name = chat_name.replace('"', '\\"').replace("'", "\\'")

            # Try to find in sidebar
            title_el = page.query_selector(f'#pane-side [title="{safe_name}"]')

            if not title_el:
                # Try partial match in sidebar
                title_el = page.query_selector(f'#pane-side span[title*="{safe_name[:20]}"]')

            if not title_el:
                logger.warning(f"Bot {bot_profile_id}: Could not find chat in sidebar: {chat_name}")
                # Mark as synced anyway to avoid blocking
                with get_db_session() as db:
                    db.query(Conversation).filter(Conversation.id == conversation_id).update({
                        "history_synced": True,
                        "last_synced_at": datetime.utcnow()
                    })
                    db.commit()
                return 0

            title_el.click()
            time.sleep(1.5)

            # Re-check for conversation panel
            conv_panel = page.query_selector('[data-testid="conversation-panel-messages"]') or \
                        page.query_selector('#main')

        if not conv_panel:
            logger.warning(f"Bot {bot_profile_id}: Chat did not open: {chat_name}")
            with get_db_session() as db:
                db.query(Conversation).filter(Conversation.id == conversation_id).update({
                    "history_synced": True,
                    "last_synced_at": datetime.utcnow()
                })
                db.commit()
            return 0

        # Scroll up to load historical messages until we have at least max_history
        # or no more messages can be loaded
        max_scroll_attempts = 20  # Safety limit to prevent infinite scrolling
        prev_message_count = 0

        # Use JavaScript to scroll to top first - more reliable than keyboard
        try:
            page.evaluate('''() => {
                const panel = document.querySelector('[data-testid="conversation-panel-messages"]') ||
                             document.querySelector('#main .copyable-area') ||
                             document.querySelector('#main');
                if (panel) {
                    panel.scrollTop = 0;
                }
            }''')
            time.sleep(0.5)
        except Exception as scroll_err:
            logger.debug(f"Bot {bot_profile_id}: Initial scroll error: {scroll_err}")

        for scroll_attempt in range(max_scroll_attempts):
            # Count current messages
            current_count = page.evaluate('''() => {
                const msgs = document.querySelectorAll('[data-testid="msg-container"], .message-in, .message-out');
                let count = 0;
                for (const msg of msgs) {
                    const text = msg.querySelector('span.selectable-text, .copyable-text, [data-testid="msg-text"]');
                    if (text && text.innerText && text.innerText.trim().length > 0) count++;
                }
                return count;
            }''')

            logger.info(f"Bot {bot_profile_id}: Scroll {scroll_attempt + 1}: {current_count} msgs, target={max_history}")

            # Stop if we have enough messages
            if current_count >= max_history:
                logger.info(f"Bot {bot_profile_id}: Reached target message count ({current_count} >= {max_history})")
                break

            # Stop if no new messages were loaded (reached top of chat)
            if current_count == prev_message_count and scroll_attempt > 0:
                logger.info(f"Bot {bot_profile_id}: No more messages to load (count stayed at {current_count})")
                break

            prev_message_count = current_count

            # Use keyboard navigation to scroll up - more reliable than scrollTop
            # Press Page Up multiple times to scroll up and trigger lazy loading
            try:
                for _ in range(3):  # Multiple Page Up presses for faster scrolling
                    page.keyboard.press('PageUp')
                    time.sleep(0.2)
            except Exception as kb_err:
                logger.debug(f"Bot {bot_profile_id}: Keyboard scroll error: {kb_err}")
                # Fallback: try JavaScript scroll
                page.evaluate('''() => {
                    const selectors = [
                        '[data-testid="conversation-panel-messages"]',
                        '#main [role="application"]',
                        '#main .copyable-area',
                        '#main'
                    ];
                    for (const selector of selectors) {
                        const panel = document.querySelector(selector);
                        if (panel) {
                            panel.scrollTop = 0;
                            break;
                        }
                    }
                }''')

            time.sleep(1.0)  # Wait for WhatsApp to load messages

        # Extract all messages
        messages_data = page.evaluate('''() => {
            const messages = [];

            // Track current date from date separators
            let currentDateStr = null;

            // Get main panel
            const mainPanel = document.querySelector('#main [role="application"]') ||
                             document.querySelector('#main .copyable-area') ||
                             document.querySelector('#main');

            if (!mainPanel) return messages;

            // Select all rows including date separators
            const allRows = mainPanel.querySelectorAll('[role="row"], .focusable-list-item');

            for (const row of allRows) {
                // Check if this is a date separator
                const isMsgContainer = row.querySelector('[data-testid="msg-container"]') ||
                                      row.classList.contains('message-in') ||
                                      row.classList.contains('message-out') ||
                                      row.querySelector('.message-in, .message-out');

                if (!isMsgContainer) {
                    // Extract date from separator
                    const dateSpan = row.querySelector('span[dir="auto"]');
                    if (dateSpan) {
                        const dateText = (dateSpan.innerText || '').trim();
                        if (/^\\d{1,2}\\/\\d{1,2}\\/\\d{2,4}$/.test(dateText)) {
                            currentDateStr = dateText;
                        } else if (/^(Today|Yesterday)$/i.test(dateText)) {
                            const today = new Date();
                            if (dateText.toLowerCase() === 'yesterday') {
                                today.setDate(today.getDate() - 1);
                            }
                            currentDateStr = (today.getMonth() + 1) + '/' + today.getDate() + '/' + today.getFullYear();
                        }
                    }
                    continue;
                }

                // Find the actual message container
                const msg = row.querySelector('[data-testid="msg-container"]') ||
                           row.querySelector('.message-in, .message-out') ||
                           (row.classList.contains('message-in') || row.classList.contains('message-out') ? row : null);

                if (!msg) continue;

                let text = '';

                // Check if this is a media message (has image/document/video)
                const hasMedia = msg.querySelector('[data-testid="image-thumb"]') ||
                                msg.querySelector('[data-testid="video-thumb"]') ||
                                msg.querySelector('[data-testid="document-thumb"]') ||
                                msg.querySelector('[data-testid="audio-play"]') ||
                                msg.querySelector('img[src*="blob:"]');

                // For media messages, try caption-specific selectors first
                if (hasMedia) {
                    const captionSelectors = [
                        '[data-testid="media-caption"] span.selectable-text',
                        '[data-testid="media-caption"]',
                    ];
                    for (const selector of captionSelectors) {
                        const captionEl = msg.querySelector(selector);
                        if (captionEl) {
                            text = (captionEl.innerText || '').trim();
                            if (text) break;
                        }
                    }
                }

                // Fallback to standard text extraction
                if (!text) {
                    const selectableText = msg.querySelector('span.selectable-text');
                    if (selectableText) text = selectableText.innerText || '';
                }
                if (!text) {
                    const copyableText = msg.querySelector('.copyable-text');
                    if (copyableText) text = copyableText.innerText || '';
                }
                if (!text) {
                    const msgText = msg.querySelector('[data-testid="msg-text"]');
                    if (msgText) text = msgText.innerText || '';
                }

                text = text.trim();

                // Check if this is a group message
                const dataIdAttr = msg.getAttribute('data-id') || '';
                const isGroupMsg = dataIdAttr.includes('@g.us');

                // For media messages in groups, clean out sender name, phone, timestamp
                if (hasMedia && text && isGroupMsg) {
                    const lines = text.split('\\n').map(l => l.trim()).filter(l => l);
                    const cleanLines = [];

                    // Get sender name to exclude it from content
                    let detectedSender = null;
                    const authorEl = msg.querySelector('[data-testid="author"]') ||
                                    msg.querySelector('[data-testid="msg-author-title"]') ||
                                    msg.querySelector('[data-testid="author-name"]');
                    if (authorEl) {
                        detectedSender = (authorEl.innerText || '').trim().toLowerCase();
                    }

                    for (const line of lines) {
                        const lineLower = line.toLowerCase();
                        // Skip if line matches sender name
                        if (detectedSender && lineLower === detectedSender) continue;
                        // Skip if line matches timestamp pattern (e.g., "8:21 AM", "14:30", "8:21 ص", "8:21 م")
                        // Include Arabic AM (ص) and PM (م)
                        if (/^\\d{1,2}:\\d{2}(\\s*([APap][Mm]|ص|م))?$/i.test(line)) continue;
                        // Skip if line matches phone number pattern (e.g., "+60 16-260 9676")
                        if (/^\\+?\\d[\\d\\s\\-()]{6,}$/.test(line)) continue;
                        // Skip common metadata patterns
                        if (/^(Yesterday|Today|\\d{1,2}\\/\\d{1,2}\\/\\d{2,4})$/i.test(line)) continue;
                        cleanLines.push(line);
                    }
                    text = cleanLines.join('\\n').trim();
                }

                if (!text || text.length < 1) continue;

                const isOutgoing = msg.classList.contains('message-out') ||
                                  msg.closest('.message-out') !== null ||
                                  msg.querySelector('[data-testid="msg-dblcheck"]') !== null ||
                                  msg.querySelector('[data-testid="msg-check"]') !== null;

                // Get sender name - try multiple selectors for group chats
                // This works for both text messages AND media messages (images/documents)
                let sender = null;
                if (!isOutgoing) {
                    // Method 1: Standard author elements
                    const authorEl = msg.querySelector('[data-testid="author"]') ||
                                    msg.querySelector('[data-testid="msg-author-title"]') ||
                                    msg.querySelector('[data-testid="author-name"]') ||
                                    msg.querySelector('span[dir="auto"][aria-label]');
                    if (authorEl) {
                        sender = authorEl.innerText || authorEl.getAttribute('aria-label') || '';
                    }

                    // Method 2: Extract from data-pre-plain-text attribute (text messages)
                    if (!sender) {
                        const copyableEl = msg.querySelector('.copyable-text[data-pre-plain-text]');
                        if (copyableEl) {
                            const preText = copyableEl.getAttribute('data-pre-plain-text') || '';
                            const match = preText.match(/\\]\\s*([^:]+):/);
                            if (match) {
                                sender = match[1].trim();
                            }
                        }
                    }

                    // Method 3: For MEDIA messages (images/documents) in groups
                    // The sender name is displayed as a colored span at the top of the message
                    if (!sender) {
                        const msgContainer = msg.closest('[data-testid="msg-container"]') || msg;
                        const allSpans = msgContainer.querySelectorAll('span[dir="auto"]');
                        for (const span of allSpans) {
                            const style = window.getComputedStyle(span);
                            const color = style.color;
                            const spanText = (span.innerText || '').trim();

                            if (spanText && spanText.length > 0 && spanText.length < 30) {
                                if (color && !color.includes('rgb(255, 255, 255)') &&
                                    !color.includes('rgba(255, 255, 255') &&
                                    !color.includes('rgb(0, 0, 0)') &&
                                    !color.includes('rgba(0, 0, 0')) {
                                    const spanRect = span.getBoundingClientRect();
                                    const msgRect = msgContainer.getBoundingClientRect();
                                    if (spanRect.top - msgRect.top < 50) {
                                        if (!spanText.match(/^\\d+:\\d+/) &&
                                            !spanText.match(/^[\\d,.]+ [KMG]?B$/i) &&
                                            !spanText.match(/^\\d+ page/i) &&
                                            spanText !== 'PDF' && spanText !== 'DOC' && spanText !== 'XLS') {
                                            sender = spanText;
                                            break;
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // Method 4: Check for sender in message row's context
                    if (!sender) {
                        const msgRow = msg.closest('[role="row"]') || msg.parentElement;
                        if (msgRow) {
                            const rowAuthor = msgRow.querySelector('[data-testid="author"]') ||
                                             msgRow.querySelector('[data-testid="msg-author-title"]');
                            if (rowAuthor) {
                                sender = rowAuthor.innerText || '';
                            }
                        }
                    }

                    // Method 5: Check aria-label on the message container
                    if (!sender) {
                        const ariaLabel = msg.getAttribute('aria-label') || '';
                        const ariaMatch = ariaLabel.match(/from\\s+([^,:.]+)/i);
                        if (ariaMatch) {
                            sender = ariaMatch[1].trim();
                        }
                    }
                }

                // Get sender profile picture (for group chats)
                let senderProfilePic = null;
                if (!isOutgoing) {
                    const msgRow = msg.closest('[data-testid="msg-container"]') ||
                                  msg.closest('[role="row"]') ||
                                  msg.closest('.message-in') ||
                                  msg.parentElement;

                    if (msgRow) {
                        const avatarSelectors = [
                            'img[data-testid="author-avatar"]',
                            '[data-testid="contact-avatar"] img',
                            '[data-testid="user-avatar"] img',
                            'div[role="button"] img[src*="pps.whatsapp.net"]',
                            'img[src*="pps.whatsapp.net"]'
                        ];

                        for (const selector of avatarSelectors) {
                            const avatar = msgRow.querySelector(selector);
                            if (avatar && avatar.src && !avatar.src.includes('default-user')) {
                                senderProfilePic = avatar.src;
                                break;
                            }
                        }

                        // Check parent for avatar
                        if (!senderProfilePic && msgRow.parentElement) {
                            const avatars = msgRow.parentElement.querySelectorAll('img');
                            for (const avatar of avatars) {
                                if (avatar.src && avatar.src.includes('pps.whatsapp.net') &&
                                    avatar.width > 20 && avatar.width < 60) {
                                    senderProfilePic = avatar.src;
                                    break;
                                }
                            }
                        }
                    }
                }

                // Extract sender WhatsApp ID and message ID from data-id attribute
                // Private chat: "false_PHONE@c.us_MSGID" - PHONE is the sender
                // Group chat: "false_GROUPID@g.us_MSGID_SENDERID@lid" - extract SENDERID from @lid
                // Note: dataIdAttr already declared above at line 6583
                let senderWhatsappId = null;
                let messageWhatsappId = null;
                const isGroupMsg = dataIdAttr.includes('@g.us');

                if (dataIdAttr) {
                    const parts = dataIdAttr.split('_');
                    if (parts.length >= 2) {
                        if (isGroupMsg) {
                            // For group messages: extract sender's LID (the part with @lid)
                            // and message ID (long alphanumeric string without @)
                            for (let i = parts.length - 1; i >= 0; i--) {
                                if (parts[i].includes('@lid')) {
                                    senderWhatsappId = parts[i].replace('@lid', '');
                                } else if (!parts[i].includes('@') && parts[i].length > 10) {
                                    // This is likely the message ID (long alphanumeric string)
                                    messageWhatsappId = parts[i];
                                }
                            }
                        } else {
                            // For private chat: find the part containing @c.us or @lid (sender's phone/ID)
                            for (let i = 1; i < parts.length; i++) {
                                if (parts[i].includes('@c.us')) {
                                    senderWhatsappId = parts[i].replace('@c.us', '');
                                } else if (parts[i].includes('@lid')) {
                                    // WhatsApp 2025+ format for private chats
                                    senderWhatsappId = parts[i].replace('@lid', '');
                                } else if (!parts[i].includes('@') && parts[i].length > 10) {
                                    // This is likely the message ID (long alphanumeric string)
                                    messageWhatsappId = parts[i];
                                }
                            }
                        }
                    }
                }

                // Extract phone from message header (Business WhatsApp shows phone in header)
                // Look for: <span class="_ahx_" role="button">+60 16-260 9676</span>
                let senderPhoneFromHeader = null;
                if (!isOutgoing) {
                    const phoneSpan = msg.querySelector('span._ahx_[role="button"]') ||
                                     msg.querySelector('span[class*="_ahx"]');
                    if (phoneSpan) {
                        const phoneText = (phoneSpan.innerText || '').trim();
                        if (/^\\+?\\d[\\d\\s\\-()]{6,}$/.test(phoneText)) {
                            senderPhoneFromHeader = phoneText.replace(/[\\s\\-()]/g, '');
                        }
                    }
                }

                // Get full timestamp from data-pre-plain-text attribute
                // ONLY use timestamps that include both date and time - skip messages without valid timestamps
                let fullTimestamp = null;
                let preTextDebug = null;
                const copyableTextTs = msg.querySelector('.copyable-text[data-pre-plain-text]');
                if (copyableTextTs) {
                    const preText = copyableTextTs.getAttribute('data-pre-plain-text') || '';
                    preTextDebug = preText.substring(0, 100);
                    // Extract full timestamp with date - this is the ONLY reliable source
                    const tsMatch = preText.match(/\\[(\\d{1,2}:\\d{2}(?:\\s*(?:[AP]M|ص|م))?),\\s*(\\d{1,2}\\/\\d{1,2}\\/\\d{2,4})\\]/i);
                    if (tsMatch) {
                        let timeStr = tsMatch[1].trim();
                        timeStr = timeStr.replace(/\\s*ص$/, ' AM').replace(/\\s*م$/, ' PM');
                        fullTimestamp = timeStr + ', ' + tsMatch[2].trim();
                    }
                }

                // Skip messages without valid full timestamp (date + time)
                if (!fullTimestamp) {
                    continue;
                }

                messages.push({
                    text: text.substring(0, 2000),
                    isOutgoing: isOutgoing,
                    isGroupMsg: isGroupMsg,
                    sender: sender,
                    senderProfilePic: senderProfilePic,
                    senderWhatsappId: senderWhatsappId,
                    senderPhoneFromHeader: senderPhoneFromHeader,
                    messageWhatsappId: messageWhatsappId,
                    fullTimestamp: fullTimestamp,
                    preTextDebug: preTextDebug,
                    messageIndex: messages.length,
                });
            }
            return messages;
        }''')

        # Store messages in database
        added_count = 0
        with get_db_session() as db:
            if messages_data and len(messages_data) > 0:
                # Sort messages by messageIndex to ensure correct chronological order
                # messageIndex captures DOM order (top=oldest to bottom=newest)
                messages_data.sort(key=lambda m: m.get('messageIndex', 0))

                # Get conversation info FIRST to check if it's a group
                conv_info = db.query(Conversation).filter(Conversation.id == conversation_id).first()
                is_group_conv = conv_info.is_group if conv_info else False
                conv_chat_name = conv_info.chat_name if conv_info else chat_name
                conv_profile_pic = conv_info.profile_pic if conv_info else None

                # Get existing messages ONLY to find earliest timestamp for ordering
                # NO content-based duplicate detection - same message can be sent multiple times!
                existing_messages = db.query(Message).filter(
                    Message.conversation_id == conversation_id
                ).all()

                # Find earliest existing timestamp for historical ordering
                earliest_existing = None
                if existing_messages:
                    timestamps = [m.timestamp for m in existing_messages if m.timestamp]
                    if timestamps:
                        earliest_existing = min(timestamps)

                # Cache for sender profile pics from private conversations
                sender_profile_pics = {}

                # Find the last user message that has NO response after it
                # These are "new incoming" messages that should NOT be stored in history
                last_responded_user_idx = -1
                for idx in range(len(messages_data) - 1, -1, -1):
                    msg = messages_data[idx]
                    is_outgoing = msg.get('isOutgoing', False)
                    if is_outgoing:
                        # This is an assistant response, mark the previous user as "responded"
                        last_responded_user_idx = idx
                        break

                for idx, msg in enumerate(messages_data):
                    role = "assistant" if msg.get('isOutgoing') else "user"
                    content = msg.get('text', '')

                    # NO content-based duplicate detection!
                    # Same message can be sent multiple times by the same person
                    # The history_synced flag prevents re-syncing the same conversation

                    # Skip user messages after the last assistant response
                    # These are "new incoming" messages - let normal processing handle them
                    if role == "user" and idx > last_responded_user_idx and last_responded_user_idx >= 0:
                        logger.debug(f"Bot {bot_profile_id}: Skipping new incoming message from history sync: {content[:30]}...")
                        continue

                    # Parse actual timestamp from WhatsApp (fullTimestamp has "HH:MM, DD/MM/YYYY")
                    # Debug: log the raw preText for analysis
                    if msg.get('preTextDebug'):
                        logger.info(f"Bot {bot_profile_id}: TIMESTAMP DEBUG - preText='{msg.get('preTextDebug')}', fullTimestamp='{msg.get('fullTimestamp')}'")

                    # Use timezone_offset to convert to UTC if available
                    msg_timestamp = _parse_whatsapp_timestamp(msg.get('fullTimestamp'), None, timezone_offset)

                    # Fallback: use order-based timestamp if no actual timestamp available
                    if not msg_timestamp:
                        if earliest_existing:
                            msg_timestamp = earliest_existing - timedelta(minutes=len(messages_data) - idx)
                        else:
                            msg_timestamp = datetime.utcnow() - timedelta(minutes=len(messages_data) - idx)

                    # Get sender name, profile pic, and WhatsApp ID
                    sender_name = msg.get('sender')
                    sender_pic = msg.get('senderProfilePic')  # Use extracted pic first
                    sender_whatsapp_id = _normalize_sender_id(msg.get('senderWhatsappId'))  # Normalize ID

                    if role == "user":
                        if is_group_conv:
                            # For group chats, use extracted sender name and pic
                            if sender_name:
                                # If no pic extracted from message, check cache or private conversation
                                if not sender_pic:
                                    if sender_name in sender_profile_pics:
                                        sender_pic = sender_profile_pics[sender_name]
                                    else:
                                        # Look up sender's private conversation
                                        sender_private_conv = db.query(Conversation).filter(
                                            Conversation.bot_profile_id == bot_profile_id,
                                            Conversation.is_group == False,
                                            Conversation.chat_name == sender_name
                                        ).first()
                                        if sender_private_conv and sender_private_conv.profile_pic:
                                            sender_pic = sender_private_conv.profile_pic
                                        sender_profile_pics[sender_name] = sender_pic
                                else:
                                    # Cache the extracted pic for future use
                                    sender_profile_pics[sender_name] = sender_pic
                        else:
                            # For private chats, sender is the contact (chat_name)
                            sender_name = conv_chat_name
                            sender_pic = conv_profile_pic
                            # For private chats, try to extract sender_id from conversation's chat_id
                            if not sender_whatsapp_id and conv_info and conv_info.chat_id:
                                # Extract phone from chat_id (e.g., "971524906816@c.us" -> "971524906816")
                                sender_whatsapp_id = _normalize_sender_id(conv_info.chat_id)
                            logger.debug(f"Bot {bot_profile_id}: Private chat sender: {sender_name} (from conv_chat_name)")
                    else:
                        # For assistant messages, use bot's WhatsApp profile pic and phone
                        sender_name = "AI Agent"
                        bot = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
                        if bot:
                            # Use bot's WhatsApp profile pic if available
                            if bot.whatsapp_profile_pic:
                                sender_pic = bot.whatsapp_profile_pic
                            else:
                                sender_pic = AI_AGENT_PROFILE_PIC
                            # Use bot's phone as sender_id (without @c.us suffix)
                            if bot.whatsapp_phone:
                                sender_whatsapp_id = bot.whatsapp_phone.replace('+', '').replace(' ', '').replace('-', '')
                        else:
                            sender_pic = AI_AGENT_PROFILE_PIC

                    # Get WhatsApp message ID for tracking
                    whatsapp_msg_id = msg.get('messageWhatsappId')

                    # Get sender phone from header (Business WhatsApp) and clean it
                    sender_phone = msg.get('senderPhoneFromHeader')
                    sender_phone_clean = None
                    if sender_phone:
                        phone_clean = sender_phone.replace('+', '').replace(' ', '').replace('-', '')
                        if phone_clean.isdigit() and len(phone_clean) >= 7:
                            sender_phone_clean = phone_clean

                    new_msg = Message(
                        conversation_id=conversation_id,
                        role=role,
                        content=content,
                        sender_name=sender_name,
                        sender_id=sender_whatsapp_id,  # Phone number only (no @c.us/@g.us)
                        sender_phone=sender_phone_clean,
                        sender_profile_pic=sender_pic,
                        whatsapp_message_id=whatsapp_msg_id,
                        timestamp=msg_timestamp
                    )
                    db.add(new_msg)
                    added_count += 1

            # Mark conversation as synced
            conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
            if conv:
                conv.history_synced = True
                conv.last_synced_at = datetime.utcnow()
                conv.message_count = (conv.message_count or 0) + added_count

            db.commit()

        logger.info(f"Bot {bot_profile_id}: On-demand sync complete for '{chat_name}' - {added_count} messages added")

        # NOTE: Do NOT navigate away - we need to stay in the chat to send the AI reply
        return added_count

    except Exception as e:
        logger.error(f"Bot {bot_profile_id}: Error in on-demand sync for {chat_name}: {e}")
        # Mark as synced to avoid blocking
        with get_db_session() as db:
            db.query(Conversation).filter(Conversation.id == conversation_id).update({
                "history_synced": True,
                "last_synced_at": datetime.utcnow()
            })
            db.commit()
        return 0


def _quick_sync_conversations(page, bot_profile_id, notify_status):
    """
    Quick sync: Only extract conversations with UNREAD messages from sidebar.
    Does NOT extract messages - history is synced on-demand when responding to a conversation.
    This makes initial load much faster.
    """
    from app.database import get_db_session, Conversation
    import time

    logger.info(f"Bot {bot_profile_id}: Starting quick conversation sync (unread only)...")
    notify_status({"status": "syncing", "message": "Loading conversations with unread messages..."})

    time.sleep(3)  # Give sidebar time to fully load with contact names

    # Extract ONLY conversations with unread messages from sidebar
    sidebar_data = page.evaluate('''async () => {
        const conversations = [];
        const processedNames = new Set();  // Track processed names to avoid duplicates

        // Wait a bit for DOM to stabilize
        await new Promise(r => setTimeout(r, 500));

        // Use broader selector set for compatibility with both WhatsApp Personal and Business
        // listitem (Personal), row (Business), cell-frame-container (fallback)
        const chatRows = document.querySelectorAll(
            '#pane-side [role="listitem"], #pane-side [role="row"], [data-testid="cell-frame-container"]'
        );

        for (const row of chatRows) {
            // Check for unread message indicator - ONLY process if has unread
            const unreadBadge = row.querySelector('[data-testid="icon-unread-count"]') ||
                               row.querySelector('span[aria-label*="unread"]');

            // SKIP conversations without unread messages
            if (!unreadBadge) continue;

            // Find the chat name element - WhatsApp shows contact name here
            // IMPORTANT: Be very specific to avoid matching message preview text
            // The chat name is in cell-frame-title, NOT in the message preview area
            let nameSpan = row.querySelector('[data-testid="cell-frame-title"] span[title]');

            // Fallback: find first span[title] that is NOT in message preview area
            if (!nameSpan) {
                const allTitleSpans = row.querySelectorAll('span[title]');
                for (const span of allTitleSpans) {
                    // Skip if this span is inside a message preview container
                    const isInMessagePreview = span.closest('[data-testid="cell-frame-secondary"]') ||
                                              span.closest('[data-testid="last-msg-status"]') ||
                                              span.closest('[data-testid="msg-time"]');
                    if (isInMessagePreview) continue;

                    // Skip if the title is too long (likely message preview, not a name)
                    const title = span.getAttribute('title');
                    if (title && title.length <= 100) {
                        nameSpan = span;
                        break;
                    }
                }
            }

            if (!nameSpan) continue;

            // Get the title attribute - this is what WhatsApp displays as the chat name
            const chatName = nameSpan.getAttribute('title');

            if (!chatName || processedNames.has(chatName)) continue;
            processedNames.add(chatName);

            // Extract FULL data-id - CRITICAL: must match the correct chat
            // WhatsApp structure: listitem > div[role="row"] or div with data-id
            let fullDataId = null;

            // Method 1: Check the row element itself
            let dataId = row.getAttribute('data-id');
            if (dataId && (dataId.includes('@c.us') || dataId.includes('@g.us'))) {
                fullDataId = dataId;
            }

            // Method 2: Check direct child with role="row" (most reliable in WhatsApp)
            if (!fullDataId) {
                const rowChild = row.querySelector(':scope > div[data-id]');
                if (rowChild) {
                    const id = rowChild.getAttribute('data-id');
                    if (id && (id.includes('@c.us') || id.includes('@g.us'))) {
                        fullDataId = id;
                    }
                }
            }

            // Method 3: Check first-level children only (avoid nested elements)
            if (!fullDataId) {
                for (const child of row.children) {
                    const id = child.getAttribute('data-id');
                    if (id && (id.includes('@c.us') || id.includes('@g.us'))) {
                        fullDataId = id;
                        break;
                    }
                }
            }

            // Method 4: Last resort - find any data-id but log warning
            if (!fullDataId) {
                const dataIdElements = row.querySelectorAll('[data-id]');
                for (const elem of dataIdElements) {
                    const id = elem.getAttribute('data-id');
                    if (id && (id.includes('@c.us') || id.includes('@g.us'))) {
                        fullDataId = id;
                        console.warn('Found data-id via fallback for:', chatName, 'id:', id);
                        break;
                    }
                }
            }

            // Method 5: WhatsApp Business - check parent/ancestor for data-id
            if (!fullDataId) {
                let parent = row.parentElement;
                for (let i = 0; i < 3 && parent; i++) {
                    const id = parent.getAttribute('data-id');
                    if (id && (id.includes('@c.us') || id.includes('@g.us'))) { fullDataId = id; break; }
                    parent = parent.parentElement;
                }
            }

            // Check if group based on ICON presence (visual indicator)
            // Look for group icon in the avatar area - enhanced for WhatsApp Business
            const avatarArea = row.querySelector('[data-testid="cell-frame-primary"]') || row;
            const hasGroupIcon = avatarArea.querySelector('[data-icon="default-group"]') !== null ||
                                avatarArea.querySelector('[data-testid="default-group"]') !== null ||
                                avatarArea.querySelector('span[data-icon="default-group"]') !== null ||
                                avatarArea.querySelector('[data-icon="group"]') !== null ||
                                avatarArea.querySelector('span[data-icon*="group"]') !== null ||
                                row.querySelector('[data-testid="group-icon"]') !== null ||
                                row.querySelector('[aria-label*="group"]') !== null ||
                                row.querySelector('[aria-label*="Group"]') !== null;

            // IMPORTANT: Group detection - @g.us means group, @c.us means private
            // Priority: data-id suffix is the SOURCE OF TRUTH
            let isGroup = false;
            if (fullDataId) {
                isGroup = fullDataId.includes('@g.us');
            } else {
                // Fallback to icon only if no data-id
                isGroup = hasGroupIcon;
            }

            // Debug logging
            console.log('Chat extraction:', chatName, 'dataId:', fullDataId, 'isGroup:', isGroup, 'hasGroupIcon:', hasGroupIcon);

            // Get profile picture
            let profilePic = null;
            const img = row.querySelector('img[draggable="false"]');
            if (img && img.src && !img.src.includes('default-user') && !img.src.includes('default-group')) {
                try {
                    const response = await fetch(img.src);
                    const blob = await response.blob();
                    profilePic = await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result);
                        reader.onerror = reject;
                        reader.readAsDataURL(blob);
                    });
                } catch(e) {}
            }

            conversations.push({
                name: chatName,
                fullDataId: fullDataId,
                isGroup: isGroup,
                profilePic: profilePic
            });
        }

        return conversations;
    }''')

    # sidebar_data now ONLY contains unread conversations (filtered in JavaScript)
    logger.info(f"Bot {bot_profile_id}: Found {len(sidebar_data)} conversations with unread messages")

    # Debug: Log what was extracted
    for i, conv in enumerate(sidebar_data[:10]):  # Log first 10
        logger.info(f"Bot {bot_profile_id}: Unread conv {i}: name='{conv.get('name')}', fullDataId='{conv.get('fullDataId')}', isGroup={conv.get('isGroup')}")

    # Store unread conversations in database
    synced_count = 0
    with get_db_session() as db:
        for conv_info in sidebar_data:
            sidebar_name = conv_info.get('name')  # What WhatsApp shows in sidebar
            chat_data_id = conv_info.get('fullDataId')  # Full data-id extracted from WhatsApp
            is_group = conv_info.get('isGroup', False)
            profile_pic = conv_info.get('profilePic')

            if not sidebar_name:
                continue

            # Use FULL DATA-ID as chat_id ONLY if valid (e.g., "971524906816@c.us")
            chat_id = chat_data_id if _is_valid_whatsapp_id(chat_data_id) else None

            chat_name = sidebar_name  # Display name shown in sidebar

            # Determine if sidebar shows a name or phone number
            is_phone_format = bool(re.match(r'^[\+\d\s\-\(\)]+$', sidebar_name.strip()))

            # If sidebar shows a name (not phone), that's the display_name
            if not is_phone_format and not is_group:
                display_name = sidebar_name
            else:
                display_name = None

            conversation = None

            # First, try to find by valid chat_id
            if chat_id:
                conversation = db.query(Conversation).filter(
                    Conversation.bot_profile_id == bot_profile_id,
                    Conversation.chat_id == chat_id
                ).first()

            # If not found, search by name for any existing conversation with valid WhatsApp ID
            if not conversation:
                all_matching = db.query(Conversation).filter(
                    Conversation.bot_profile_id == bot_profile_id,
                    Conversation.chat_name == chat_name
                ).all()

                # Prefer conversations with valid WhatsApp IDs
                for existing in all_matching:
                    if _is_valid_whatsapp_id(existing.chat_id):
                        conversation = existing
                        break

                # If no valid-ID conversation, use any existing
                if not conversation and all_matching:
                    conversation = all_matching[0]

            # Only create new conversation if we have a valid WhatsApp ID
            if not conversation:
                if not _is_valid_whatsapp_id(chat_data_id):
                    logger.warning(f"Bot {bot_profile_id}: Skipping conversation '{chat_name}' - no valid WhatsApp ID (got: {chat_data_id})")
                    continue  # Skip this conversation - don't create with invalid ID

                conversation = Conversation(
                    bot_profile_id=bot_profile_id,
                    chat_id=chat_data_id,  # Use validated chat_data_id
                    chat_name=chat_name,
                    display_name=display_name,
                    is_group=is_group,
                    profile_pic=profile_pic
                )
                db.add(conversation)
                logger.info(f"Bot {bot_profile_id}: Created conversation: {chat_name} (ID: {chat_data_id})")
            else:
                # Update existing conversation with full data-id ONLY if valid
                if _is_valid_whatsapp_id(chat_data_id) and conversation.chat_id != chat_data_id:
                    logger.info(f"Bot {bot_profile_id}: Updating chat_id from '{conversation.chat_id}' to valid WhatsApp ID '{chat_data_id}'")
                    conversation.chat_id = chat_data_id
                if display_name and not conversation.display_name:
                    conversation.display_name = display_name
                if chat_name and conversation.chat_name != chat_name:
                    conversation.chat_name = chat_name
                if profile_pic and not conversation.profile_pic:
                    conversation.profile_pic = profile_pic
                if is_group != conversation.is_group:
                    conversation.is_group = is_group

            synced_count += 1

        db.commit()

    logger.info(f"Bot {bot_profile_id}: Quick sync complete - {synced_count} unread conversations")
    notify_status({"status": "running", "message": f"Loaded {synced_count} conversations with unread messages. Ready!"})

    return synced_count


def _sync_messages_batch(page, bot_profile_id, _unused=None, batch_size=5):
    """
    Sync messages for conversations that haven't had their history synced yet.
    Only fetches full history ONCE per conversation (uses history_synced flag in DB).
    After history is synced, new messages are captured via the message listener.
    """
    from app.database import get_db_session, Conversation, Message, BotProfile
    from datetime import datetime, timedelta
    import time

    # Get bot's max_history setting and timezone offset
    max_history = 20  # Default
    timezone_offset = None  # Will use detected offset if available
    with get_db_session() as db:
        bot = db.query(BotProfile).filter(BotProfile.id == bot_profile_id).first()
        if bot:
            max_history = bot.max_history or 20
            timezone_offset = bot.whatsapp_timezone_offset  # May be None if not yet detected

    # Get conversations that need history sync (history_synced = False)
    with get_db_session() as db:
        # Find conversations where history hasn't been synced yet
        to_sync = db.query(Conversation).filter(
            Conversation.bot_profile_id == bot_profile_id,
            Conversation.history_synced == False
        ).limit(batch_size).all()

        if not to_sync:
            logger.debug(f"Bot {bot_profile_id}: All conversations have history synced")
            return 0

        # Extract data before session closes
        to_sync_data = [{
            'id': conv.id,
            'chat_id': conv.chat_id,
            'chat_name': conv.chat_name,
            'is_group': conv.is_group,
            'profile_pic': conv.profile_pic
        } for conv in to_sync]

    logger.info(f"Bot {bot_profile_id}: Syncing history for {len(to_sync_data)} conversations")

    # Sync each conversation
    synced_count = 0
    for conv_info in to_sync_data:
        chat_name = conv_info['chat_name']
        conv_id = conv_info['id']
        is_group = conv_info.get('is_group', False)
        conv_profile_pic = conv_info.get('profile_pic')

        try:
            # Click on the chat to open it
            safe_name = chat_name.replace('"', '\\"')
            title_el = page.query_selector(f'[title="{safe_name}"]')

            if not title_el:
                logger.debug(f"Bot {bot_profile_id}: Could not find chat in sidebar: {chat_name}")
                # Mark as synced to avoid retrying repeatedly
                with get_db_session() as db:
                    db.query(Conversation).filter(Conversation.id == conv_id).update({
                        "history_synced": True,
                        "last_synced_at": datetime.utcnow()
                    })
                    db.commit()
                continue

            title_el.click()
            time.sleep(1.5)

            # Verify chat opened
            conv_panel = page.query_selector('[data-testid="conversation-panel-messages"]') or \
                        page.query_selector('#main')

            if not conv_panel:
                logger.debug(f"Bot {bot_profile_id}: Chat did not open: {chat_name}")
                with get_db_session() as db:
                    db.query(Conversation).filter(Conversation.id == conv_id).update({
                        "history_synced": True,
                        "last_synced_at": datetime.utcnow()
                    })
                    db.commit()
                continue

            # Scroll up to load historical messages until we have at least max_history
            max_scroll_attempts = 20  # Safety limit
            prev_message_count = 0

            # Use JavaScript to scroll to top first
            try:
                page.evaluate('''() => {
                    const panel = document.querySelector('[data-testid="conversation-panel-messages"]') ||
                                 document.querySelector('#main .copyable-area') ||
                                 document.querySelector('#main');
                    if (panel) panel.scrollTop = 0;
                }''')
                time.sleep(0.5)
            except Exception:
                pass

            for scroll_attempt in range(max_scroll_attempts):
                # Count current messages
                current_count = page.evaluate('''() => {
                    const msgs = document.querySelectorAll('[data-testid="msg-container"], .message-in, .message-out');
                    let count = 0;
                    for (const msg of msgs) {
                        const text = msg.querySelector('span.selectable-text, .copyable-text, [data-testid="msg-text"]');
                        if (text && text.innerText && text.innerText.trim().length > 0) count++;
                    }
                    return count;
                }''')

                # Stop if we have enough messages
                if current_count >= max_history:
                    break

                # Stop if no new messages were loaded (reached top of chat)
                if current_count == prev_message_count and scroll_attempt > 0:
                    break

                prev_message_count = current_count

                # Use keyboard navigation to scroll up - more reliable than scrollTop
                try:
                    for _ in range(3):
                        page.keyboard.press('PageUp')
                        time.sleep(0.2)
                except Exception:
                    # Fallback to JS scroll
                    page.evaluate('''() => {
                        const panel = document.querySelector('[data-testid="conversation-panel-messages"]') ||
                                     document.querySelector('#main');
                        if (panel) panel.scrollTop = 0;
                    }''')

                time.sleep(1.0)

            # Extract all messages
            messages_data = page.evaluate('''() => {
                const messages = [];

                // Track current date from date separators
                let currentDateStr = null;

                // Get main panel
                const mainPanel = document.querySelector('#main [role="application"]') ||
                                 document.querySelector('#main .copyable-area') ||
                                 document.querySelector('#main');

                if (!mainPanel) return messages;

                // Select all rows including date separators
                const allRows = mainPanel.querySelectorAll('[role="row"], .focusable-list-item');

                for (const row of allRows) {
                    // Check if this is a date separator
                    const isMsgContainer = row.querySelector('[data-testid="msg-container"]') ||
                                          row.classList.contains('message-in') ||
                                          row.classList.contains('message-out') ||
                                          row.querySelector('.message-in, .message-out');

                    if (!isMsgContainer) {
                        // Extract date from separator
                        const dateSpan = row.querySelector('span[dir="auto"]');
                        if (dateSpan) {
                            const dateText = (dateSpan.innerText || '').trim();
                            if (/^\\d{1,2}\\/\\d{1,2}\\/\\d{2,4}$/.test(dateText)) {
                                currentDateStr = dateText;
                            } else if (/^(Today|Yesterday)$/i.test(dateText)) {
                                const today = new Date();
                                if (dateText.toLowerCase() === 'yesterday') {
                                    today.setDate(today.getDate() - 1);
                                }
                                currentDateStr = (today.getMonth() + 1) + '/' + today.getDate() + '/' + today.getFullYear();
                            }
                        }
                        continue;
                    }

                    // Find the actual message container
                    const msg = row.querySelector('[data-testid="msg-container"]') ||
                               row.querySelector('.message-in, .message-out') ||
                               (row.classList.contains('message-in') || row.classList.contains('message-out') ? row : null);

                    if (!msg) continue;

                    // Check if this is a media message (has image/document/video)
                    const hasMedia = msg.querySelector('[data-testid="image-thumb"]') ||
                                    msg.querySelector('[data-testid="video-thumb"]') ||
                                    msg.querySelector('[data-testid="document-thumb"]') ||
                                    msg.querySelector('[data-testid="audio-play"]') ||
                                    msg.querySelector('img[src*="blob:"]');

                    // Check if this is a group message
                    const dataIdAttr = msg.getAttribute('data-id') || '';
                    const isGroupMsg = dataIdAttr.includes('@g.us');

                    let text = '';
                    const selectableText = msg.querySelector('span.selectable-text');
                    if (selectableText) text = selectableText.innerText || '';
                    if (!text) {
                        const copyableText = msg.querySelector('.copyable-text');
                        if (copyableText) text = copyableText.innerText || '';
                    }
                    if (!text) {
                        const msgText = msg.querySelector('[data-testid="msg-text"]');
                        if (msgText) text = msgText.innerText || '';
                    }

                    text = text.trim();

                    // For media messages in groups, clean out sender name, phone, timestamp
                    if (hasMedia && text && isGroupMsg) {
                        const lines = text.split('\\n').map(l => l.trim()).filter(l => l);
                        const cleanLines = [];

                        // Get sender name to exclude it from content
                        let detectedSender = null;
                        const authorEl = msg.querySelector('[data-testid="author"]') ||
                                        msg.querySelector('[data-testid="msg-author-title"]') ||
                                        msg.querySelector('[data-testid="author-name"]');
                        if (authorEl) {
                            detectedSender = (authorEl.innerText || '').trim().toLowerCase();
                        }

                        for (const line of lines) {
                            const lineLower = line.toLowerCase();
                            // Skip if line matches sender name
                            if (detectedSender && lineLower === detectedSender) continue;
                            // Skip if line matches timestamp pattern (e.g., "8:21 AM", "14:30", "8:21 ص", "8:21 م")
                            if (/^\\d{1,2}:\\d{2}(\\s*([APap][Mm]|ص|م))?$/i.test(line)) continue;
                            // Skip if line matches phone number pattern (e.g., "+60 16-260 9676")
                            if (/^\\+?\\d[\\d\\s\\-()]{6,}$/.test(line)) continue;
                            // Skip common metadata patterns
                            if (/^(Yesterday|Today|\\d{1,2}\\/\\d{1,2}\\/\\d{2,4})$/i.test(line)) continue;
                            cleanLines.push(line);
                        }
                        text = cleanLines.join('\\n').trim();
                    }

                    if (!text || text.length < 1) continue;

                    const isOutgoing = msg.classList.contains('message-out') ||
                                      msg.closest('.message-out') !== null ||
                                      msg.querySelector('[data-testid="msg-dblcheck"]') !== null ||
                                      msg.querySelector('[data-testid="msg-check"]') !== null;

                    // Get sender name - try multiple selectors
                    let sender = null;
                    const authorEl = msg.querySelector('[data-testid="author"]') ||
                                    msg.querySelector('[data-testid="msg-author-title"]') ||
                                    msg.querySelector('[data-testid="author-name"]') ||
                                    msg.querySelector('span[dir="auto"][aria-label]');
                    if (authorEl) {
                        sender = authorEl.innerText || authorEl.getAttribute('aria-label') || '';
                    }
                    // Fallback: extract from data-pre-plain-text attribute
                    if (!sender) {
                        const copyableEl = msg.querySelector('.copyable-text[data-pre-plain-text]');
                        if (copyableEl) {
                            const preText = copyableEl.getAttribute('data-pre-plain-text') || '';
                            const match = preText.match(/\\]\\s*([^:]+):/);
                            if (match) {
                                sender = match[1].trim();
                            }
                        }
                    }

                    // Get sender profile picture (for group chats)
                    let senderProfilePic = null;
                    const msgRow = msg.closest('[data-testid="msg-container"]') ||
                                  msg.closest('[role="row"]') ||
                                  msg.closest('.message-in') ||
                                  msg.parentElement;

                    if (msgRow && !isOutgoing) {
                        const avatarSelectors = [
                            'img[data-testid="author-avatar"]',
                            '[data-testid="contact-avatar"] img',
                            '[data-testid="user-avatar"] img',
                            'div[role="button"] img[src*="pps.whatsapp.net"]',
                            'div[role="button"] img[src*="blob:"]',
                            'img[src*="pps.whatsapp.net"]',
                            'img[src*="web.whatsapp.com/pp"]',
                            'img[draggable="false"]'
                        ];

                        for (const selector of avatarSelectors) {
                            const avatar = msgRow.querySelector(selector);
                            if (avatar && avatar.src) {
                                const src = avatar.src;
                                if (src.includes('pps.whatsapp.net') ||
                                    src.includes('web.whatsapp.com/pp') ||
                                    (src.includes('blob:') && avatar.width < 100 && avatar.width > 20)) {
                                    senderProfilePic = src;
                                    break;
                                }
                            }
                        }

                        if (!senderProfilePic && msgRow.previousElementSibling) {
                            const avatar = msgRow.previousElementSibling.querySelector('img[src*="pps.whatsapp.net"]') ||
                                          msgRow.previousElementSibling.querySelector('img[src*="blob:"]');
                            if (avatar && avatar.src) senderProfilePic = avatar.src;
                        }

                        if (!senderProfilePic && msgRow.parentElement) {
                            const avatars = msgRow.parentElement.querySelectorAll('img');
                            for (const avatar of avatars) {
                                if (avatar.src &&
                                    (avatar.src.includes('pps.whatsapp.net') || avatar.src.includes('blob:')) &&
                                    avatar.width > 20 && avatar.width < 60) {
                                    senderProfilePic = avatar.src;
                                    break;
                                }
                            }
                        }
                    }

                    // Get full timestamp from data-pre-plain-text attribute
                    // ONLY use timestamps that include both date and time - skip messages without valid timestamps
                    let fullTimestamp = null;
                    const copyableTextTs = msg.querySelector('.copyable-text[data-pre-plain-text]');
                    if (copyableTextTs) {
                        const preText = copyableTextTs.getAttribute('data-pre-plain-text') || '';
                        // Extract full timestamp with date - this is the ONLY reliable source
                        const tsMatch = preText.match(/\\[(\\d{1,2}:\\d{2}(?:\\s*(?:[AP]M|ص|م))?),\\s*(\\d{1,2}\\/\\d{1,2}\\/\\d{2,4})\\]/i);
                        if (tsMatch) {
                            let timeStr = tsMatch[1].trim();
                            timeStr = timeStr.replace(/\\s*ص$/, ' AM').replace(/\\s*م$/, ' PM');
                            fullTimestamp = timeStr + ', ' + tsMatch[2].trim();
                        }
                    }

                    // Skip messages without valid full timestamp (date + time)
                    if (!fullTimestamp) {
                        continue;
                    }

                    messages.push({
                        text: text.substring(0, 2000),
                        isOutgoing: isOutgoing,
                        sender: sender,
                        senderProfilePic: senderProfilePic,
                        fullTimestamp: fullTimestamp,
                        messageIndex: messages.length,
                    });
                }
                return messages;
            }''')

            # Store messages and mark as synced
            with get_db_session() as db:
                if messages_data and len(messages_data) > 0:
                    # Get existing messages to avoid duplicates
                    existing_messages = db.query(Message).filter(
                        Message.conversation_id == conv_id
                    ).all()

                    # Create a set of existing message content for deduplication
                    existing_content = set()
                    for em in existing_messages:
                        key = f"{em.role}:{em.content[:100]}"
                        existing_content.add(key)

                    # Find the earliest existing message timestamp
                    earliest_existing = None
                    if existing_messages:
                        timestamps = [m.timestamp for m in existing_messages if m.timestamp]
                        if timestamps:
                            earliest_existing = min(timestamps)

                    # Find the last assistant response index - messages after this are "new incoming"
                    last_responded_user_idx = -1
                    for idx in range(len(messages_data) - 1, -1, -1):
                        if messages_data[idx].get('isOutgoing', False):
                            last_responded_user_idx = idx
                            break

                    # Cache for sender profile pics
                    sender_profile_pics = {}

                    # Sort messages by messageIndex to ensure correct chronological order
                    messages_data.sort(key=lambda m: m.get('messageIndex', 0))

                    added_count = 0
                    for idx, msg in enumerate(messages_data):
                        role = "assistant" if msg.get('isOutgoing') else "user"
                        content = msg.get('text', '')

                        # Check for duplicate
                        key = f"{role}:{content[:100]}"
                        if key in existing_content:
                            continue  # Skip duplicate

                        # Skip user messages after the last assistant response (new incoming)
                        if role == "user" and idx > last_responded_user_idx and last_responded_user_idx >= 0:
                            continue

                        # Parse actual timestamp from WhatsApp (fullTimestamp has "HH:MM, DD/MM/YYYY")
                        # Use timezone_offset to convert to UTC if available
                        msg_timestamp = _parse_whatsapp_timestamp(msg.get('fullTimestamp'), None, timezone_offset)

                        # Fallback: use order-based timestamp if no actual timestamp available
                        if not msg_timestamp:
                            msg_timestamp = datetime.utcnow() - timedelta(minutes=len(messages_data) - idx)

                        # If this is a historical message, ensure it's before existing new messages
                        if msg_timestamp and earliest_existing and msg_timestamp >= earliest_existing:
                            msg_timestamp = earliest_existing - timedelta(minutes=len(messages_data) - idx)

                        # Get sender info
                        sender_pic = msg.get('senderProfilePic')
                        sender_name = msg.get('sender')
                        sender_id = _normalize_sender_id(msg.get('senderWhatsappId'))

                        if role == 'user':
                            # Set sender_name for private chats
                            if not sender_name and not is_group:
                                sender_name = chat_name

                            # For private chats, get sender_id from conversation's chat_id
                            if not sender_id and not is_group and conv_info.get('chat_id'):
                                sender_id = _normalize_sender_id(conv_info['chat_id'])

                            # Profile pic fallback logic
                            if not sender_pic:
                                # Check cache first
                                cache_key = sender_name or chat_name
                                if cache_key in sender_profile_pics:
                                    sender_pic = sender_profile_pics[cache_key]
                                elif not is_group:
                                    # For private chats, use conversation profile pic
                                    sender_pic = conv_profile_pic
                                else:
                                    # For groups, look up sender's private conversation
                                    sender_private_conv = db.query(Conversation).filter(
                                        Conversation.bot_profile_id == bot_profile_id,
                                        Conversation.is_group == False,
                                        Conversation.chat_name == sender_name
                                    ).first()
                                    if sender_private_conv and sender_private_conv.profile_pic:
                                        sender_pic = sender_private_conv.profile_pic
                                    sender_profile_pics[cache_key] = sender_pic
                            else:
                                # Cache the extracted pic for future use
                                cache_key = sender_name or chat_name
                                sender_profile_pics[cache_key] = sender_pic
                        else:
                            # For assistant messages, set sender_name and profile pic
                            sender_name = "AI Agent"
                            sender_pic = AI_AGENT_PROFILE_PIC

                        new_msg = Message(
                            conversation_id=conv_id,
                            role=role,
                            content=content,
                            sender_name=sender_name,
                            sender_id=sender_id,
                            sender_profile_pic=sender_pic,
                            timestamp=msg_timestamp
                        )
                        db.add(new_msg)
                        existing_content.add(key)
                        added_count += 1

                    if added_count > 0:
                        logger.info(f"Bot {bot_profile_id}: Added {added_count} new messages for {chat_name} (skipped {len(messages_data) - added_count} duplicates)")

                # Mark conversation as history synced and update message count
                actual_message_count = db.query(Message).filter(
                    Message.conversation_id == conv_id
                ).count()
                db.query(Conversation).filter(Conversation.id == conv_id).update({
                    "history_synced": True,
                    "last_synced_at": datetime.utcnow(),
                    "message_count": actual_message_count
                })
                db.commit()

            synced_count += 1

        except Exception as e:
            logger.error(f"Bot {bot_profile_id}: Error syncing {chat_name}: {e}")
            # Mark as synced to avoid infinite retry loop
            try:
                with get_db_session() as db:
                    db.query(Conversation).filter(Conversation.id == conv_id).update({
                        "history_synced": True,
                        "last_synced_at": datetime.utcnow()
                    })
                    db.commit()
            except:
                pass
            continue

    return synced_count


def _update_all_profile_pics(page, bot_profile_id):
    """Extract and update profile pictures for all existing conversations.
    Matches by both chat_name AND chat_id (phone number) for better accuracy.
    """
    from app.database import get_db_session, Conversation
    import time

    # Get ALL conversations for this bot
    with get_db_session() as db:
        conversations = db.query(Conversation).filter(
            Conversation.bot_profile_id == bot_profile_id
        ).all()
        # Store id, chat_id, chat_name, profile_pic
        conv_data = [(c.id, c.chat_id, c.chat_name, c.profile_pic) for c in conversations]

    if not conv_data:
        logger.info(f"Bot {bot_profile_id}: No conversations to update")
        return

    needs_update = [(id, chat_id, name) for id, chat_id, name, pic in conv_data if not pic]
    logger.info(f"Bot {bot_profile_id}: Found {len(conv_data)} conversations, {len(needs_update)} need profile pictures")
    for conv_id, chat_id, chat_name, pic in conv_data:
        status = "HAS PIC" if pic else "NEEDS PIC"
        logger.info(f"Bot {bot_profile_id}:   - '{chat_name}' (chat_id: {chat_id}) [{status}]")

    # Give sidebar time to fully load
    time.sleep(3)

    # Extract all profile pictures from sidebar - include both name and phone number
    logger.info(f"Bot {bot_profile_id}: Extracting profile pictures from sidebar...")
    all_pics = page.evaluate(r'''async () => {
        const results = {
            byName: {},      // name -> base64
            byPhone: {}      // phone -> base64
        };
        const debug = { names: [], phones: [], totalRows: 0 };

        // Find all chat rows in sidebar
        const chatRows = document.querySelectorAll('#pane-side [role="listitem"], #pane-side [role="row"], [data-testid="cell-frame-container"]');
        debug.totalRows = chatRows.length;

        for (const row of chatRows) {
            // Get display name - try multiple selectors
            const nameSpan = row.querySelector('span[title][dir="auto"]._ao3e') ||
                            row.querySelector('span._ao3e[title]') ||
                            row.querySelector('span[data-testid="cell-frame-title"] span[title]') ||
                            row.querySelector('[data-testid="cell-frame-title"] span[title]') ||
                            row.querySelector('span.ggj6brxn[title]') ||
                            row.querySelector('span[title][dir="auto"]') ||
                            row.querySelector('span[title]');

            // Get both title attribute and text content
            const titleAttr = nameSpan ? nameSpan.getAttribute('title') : null;
            const textContent = nameSpan ? nameSpan.textContent?.trim() : null;

            // Determine which is the name and which is the phone
            const isTextPhone = textContent && /^[\+\d][\d\s\-\(\)]{7,}$/.test(textContent);
            const isTitlePhone = titleAttr && /^[\+\d][\d\s\-\(\)]{7,}$/.test(titleAttr);

            // Prioritize the one that looks like a name (not a phone number)
            let chatName;
            if (textContent && !isTextPhone) {
                chatName = textContent;
            } else if (titleAttr && !isTitlePhone) {
                chatName = titleAttr;
            } else {
                chatName = titleAttr || textContent;
            }

            if (!chatName) continue;
            debug.names.push(chatName);

            // Try to extract phone number from data-id attribute or other sources
            // WhatsApp stores chat IDs in various data attributes
            let phoneNumber = null;
            const dataId = row.getAttribute('data-id') ||
                           row.querySelector('[data-id]')?.getAttribute('data-id') ||
                           row.closest('[data-id]')?.getAttribute('data-id');

            if (dataId) {
                // Extract phone from format like "true_1234567890@c.us_..."
                const match = dataId.match(/(\d{10,15})@/);
                if (match) {
                    phoneNumber = match[1];
                    debug.phones.push(phoneNumber);
                }
            }

            // If name looks like a phone number, use it
            if (!phoneNumber && /^\+?\d[\d\s-]{8,}$/.test(chatName)) {
                phoneNumber = chatName.replace(/[\s+-]/g, '');
                debug.phones.push(phoneNumber);
            }

            const img = row.querySelector('img[draggable="false"]');
            if (img && img.src && !img.src.includes('default-user')) {
                try {
                    const response = await fetch(img.src);
                    const blob = await response.blob();
                    const base64 = await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result);
                        reader.onerror = reject;
                        reader.readAsDataURL(blob);
                    });
                    results.byName[chatName] = base64;
                    if (phoneNumber) {
                        results.byPhone[phoneNumber] = base64;
                    }
                } catch(e) {
                    // Ignore fetch errors
                }
            }
        }

        return { pics: results, debug: debug };
    }''')

    # Log what we found in sidebar
    pics = all_pics.get('pics', {}) if isinstance(all_pics, dict) else {}
    debug = all_pics.get('debug', {}) if isinstance(all_pics, dict) else {}
    pics_by_name = pics.get('byName', {})
    pics_by_phone = pics.get('byPhone', {})
    sidebar_names = debug.get('names', [])

    logger.info(f"Bot {bot_profile_id}: Found {debug.get('totalRows', 0)} rows in sidebar")
    logger.info(f"Bot {bot_profile_id}: Chat names in sidebar: {sidebar_names[:15]}...")
    logger.info(f"Bot {bot_profile_id}: Extracted {len(pics_by_name)} pics by name, {len(pics_by_phone)} pics by phone")

    # Update database with extracted pictures - try matching by name first, then by phone
    updated_count = 0
    with get_db_session() as db:
        for conv_id, chat_id, chat_name in needs_update:
            pic = None
            match_type = None

            # Try exact name match first
            if chat_name in pics_by_name:
                pic = pics_by_name[chat_name]
                match_type = "name"
            # Try phone number match - extract digits from chat_id
            elif chat_id:
                phone_digits = ''.join(filter(str.isdigit, chat_id))
                # Only try phone matching if we have at least 7 digits
                if len(phone_digits) >= 7:
                    # Try matching with different phone formats
                    for phone_key in pics_by_phone:
                        if phone_digits.endswith(phone_key) or phone_key.endswith(phone_digits):
                            pic = pics_by_phone[phone_key]
                            match_type = f"phone ({phone_key})"
                            break
                    # Also try matching name that looks like phone number
                    if not pic:
                        for name, name_pic in pics_by_name.items():
                            name_digits = ''.join(filter(str.isdigit, name))
                            if len(name_digits) >= 7 and (phone_digits.endswith(name_digits) or name_digits.endswith(phone_digits)):
                                pic = name_pic
                                match_type = f"phone-name ({name})"
                                break

            if pic:
                db.query(Conversation).filter(Conversation.id == conv_id).update(
                    {"profile_pic": pic}
                )
                updated_count += 1
                logger.info(f"Bot {bot_profile_id}: Updated profile pic for '{chat_name}' via {match_type}")
            else:
                logger.info(f"Bot {bot_profile_id}: No pic found for '{chat_name}' (chat_id: {chat_id})")

        db.commit()

    logger.info(f"Bot {bot_profile_id}: Updated {updated_count} of {len(needs_update)} profile pictures")


async def _run_whatsapp_bot_async(instance, config, bot_profile_id, notify_status, notify_qr):
    """Async implementation for non-Windows platforms."""
    from playwright.async_api import async_playwright
    from app.database import get_db_session, BotProfile, Conversation, Message, ActivityLog
    from app.auth.utils import decrypt_string
    from app.ai.factory import get_ai_provider

    playwright = None
    context = None  # Persistent context (replaces browser)

    try:
        await asyncio.sleep(1)
        notify_status({"status": "starting", "message": "Initializing bot..."})
        logger.info(f"Bot {bot_profile_id}: Starting...")

        # Handle both decrypted (from auto-recovery) and encrypted (from routes) API key
        if 'api_key' in config:
            api_key = config['api_key']
        else:
            api_key = decrypt_string(config['api_key_encrypted'])
        ai_provider = get_ai_provider(
            config.get('ai_provider', 'openai'),
            api_key,
            model=config.get('model')
        )

        notify_status({"status": "launching", "message": "Launching browser..."})

        playwright = await async_playwright().start()

        # Get headless setting from config (default to False)
        headless_mode = bool(config.get('headless', False))
        logger.info(f"Bot {bot_profile_id}: Launching browser with headless={headless_mode}")

        # Create persistent session directory for this bot (use absolute path)
        # This stores ALL browser data (cookies, localStorage, IndexedDB, cache)
        # WhatsApp session will persist across bot restarts
        session_path = str(SESSIONS_DIR / f"bot_{bot_profile_id}")

        logger.info(f"Bot {bot_profile_id}: Using ABSOLUTE session path: {session_path}")

        # Check if this is a fresh session or existing one
        # Only consider it existing if it has browser data (not just our 'conversations' folder)
        browser_data_markers = ['Default', 'Local State', 'First Run']
        session_exists = os.path.exists(session_path) and any(
            os.path.exists(os.path.join(session_path, marker)) for marker in browser_data_markers
        )
        if session_exists:
            logger.info(f"Bot {bot_profile_id}: Found EXISTING browser session at: {session_path} - will attempt auto-login")
        else:
            logger.info(f"Bot {bot_profile_id}: Creating FRESH browser session at: {session_path} - will show QR code")

        os.makedirs(session_path, exist_ok=True)

        # Build persistent context options
        # Extra args for headless mode to avoid detection
        browser_args = [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-blink-features=AutomationControlled'
        ]
        if headless_mode:
            browser_args.extend([
                '--disable-gpu',
                '--disable-dev-shm-usage',
                '--disable-software-rasterizer',
                '--window-size=1280,800'
            ])
        context_options = {
            "headless": headless_mode,
            "args": browser_args,
            "viewport": {'width': 1280, 'height': 800},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "locale": 'en-US',
            "timezone_id": 'America/New_York'
        }

        # Add proxy settings if enabled
        proxy_enabled = config.get('proxy_enabled', False)
        if proxy_enabled and config.get('proxy_url'):
            proxy_config = {"server": config['proxy_url']}
            # Decrypt and add credentials if provided
            if config.get('proxy_username'):
                proxy_config["username"] = decrypt_string(config['proxy_username'])
            if config.get('proxy_password'):
                proxy_config["password"] = decrypt_string(config['proxy_password'])
            context_options["proxy"] = proxy_config
            logger.info(f"Bot {bot_profile_id}: Using proxy: {config['proxy_url']}")

        # Use persistent context - automatically saves/restores ALL browser data
        # This includes cookies, localStorage, IndexedDB - everything WhatsApp needs
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=session_path,
            **context_options
        )

        await context.add_init_script('''
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        ''')

        # Get the default page or create one
        if context.pages:
            page = context.pages[0]
        else:
            page = await context.new_page()
        notify_status({"status": "loading", "message": "Loading WhatsApp Web..."})
        await page.goto("https://web.whatsapp.com", wait_until="networkidle")

        # Wait for either QR canvas or chat list to appear (up to 30 seconds)
        try:
            await page.wait_for_selector('canvas, [data-testid="chat-list"]', timeout=30000)
        except Exception as e:
            logger.warning(f"Bot {bot_profile_id}: Timeout waiting for QR/chat: {e}")

        notify_status({"status": "waiting_qr", "message": "Waiting for QR code..."})
        logger.info(f"Bot {bot_profile_id}: WhatsApp Web loaded, entering QR loop...")

        authenticated = False
        qr_sent = False
        for i in range(120):
            if not instance.is_running:
                return

            chat_list = await page.query_selector('[data-testid="chat-list"]')
            if chat_list:
                authenticated = True
                instance.whatsapp_connected = True
                notify_status({"connected": True})
                break

            qr_data = await page.evaluate('''() => {
                const canvas = document.querySelector('canvas');
                return canvas ? canvas.toDataURL('image/png') : null;
            }''')

            if qr_data and qr_data.startswith('data:image') and len(qr_data) > 1000:
                if not qr_sent:
                    logger.info(f"Bot {bot_profile_id}: QR found!")
                    qr_sent = True
                notify_qr(qr_data)

            await asyncio.sleep(1)

        if not authenticated:
            instance.error = "Authentication timeout"
            return

        # Session is automatically saved by persistent context - no manual save needed
        logger.info(f"Bot {bot_profile_id}: Authenticated! Session persisted automatically.")

        # Continue with message loop...
        # (abbreviated for brevity - same logic as sync version)

    except Exception as e:
        logger.error(f"Bot {bot_profile_id}: Error - {e}")
        instance.error = str(e)
    finally:
        instance.is_running = False
        instance.whatsapp_connected = False
        instance.browser_connected = False
        # Close context (persistent context - session data is auto-saved)
        if context:
            try:
                await context.close()
                logger.info(f"Bot {bot_profile_id}: Browser context closed, session persisted")
            except:
                pass
        if playwright:
            await playwright.stop()
