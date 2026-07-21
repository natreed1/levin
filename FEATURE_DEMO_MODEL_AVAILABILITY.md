# Model Availability Display Feature Demo

## Overview

This feature adds a visual indicator in the chat dashboard showing which LLM models are currently connected and available for use. The display provides clear, user-friendly status messages that help users understand which AI models they can interact with.

## Feature Location

The model availability display appears at the top of the chat interface, just below the description text and above the chat layout. It shows as a panel with a heading "Available Models" and lists each connected model.

## User Interface

### Visual Design
- **Panel**: Dark-themed panel with border that matches the dashboard aesthetic
- **Heading**: "Available Models" in uppercase, muted color
- **Model Items**: Each model is displayed with:
  - A green indicator dot showing the model is active
  - The model's friendly name and provider information
  - Clean, readable status text

### Status Messages

The feature displays friendly messages in the format:
- `"{Provider} ({Model Name}) is in this Chat"`
- Example: "Claude (Anthropic) (claude-sonnet-4) is in this Chat"
- Example: "Local open source (qwen3:8b) is in this Chat"

## Implementation Details

### Code Location
- **Main Implementation**: `src/analyst_ledger/dashboard.py`
- **Helper Function**: `_get_model_availability(user_id)`
- **CSS Styling**: Added in `_css()` function with `.model-status` classes
- **Integration Point**: `_chats_page()` function

### How It Works

1. **User Identification**: Uses `ANALYST_USER_ID` environment variable (defaults to "local")
2. **Model Registry Query**: Queries the `messenger.model_link` registry for enabled profiles
3. **Status Generation**: Creates friendly status messages for each available model
4. **HTML Rendering**: Injects the model status panel into the chat page HTML
5. **Graceful Fallback**: If model registry is unavailable, the UI continues to work without errors

### Environment Configuration

Set the user ID for model profile lookup:
```bash
export ANALYST_USER_ID="your_user_id"
```

If not set, defaults to "local".

## Testing

### Test Coverage
- ✅ Chat page renders correctly without models
- ✅ Model availability helper handles missing registry gracefully
- ✅ Model status section appears when models are available
- ✅ Friendly status messages are displayed correctly
- ✅ Existing chat functionality remains intact

### Running Tests
```bash
cd /workspace
python3 -m pytest tests/test_model_availability_display.py -v
```

### Example Output
```
============================== test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.1, pluggy-1.6.0
tests/test_model_availability_display.py::test_chats_page_renders_without_models PASSED
tests/test_model_availability_display.py::test_model_availability_helper_handles_missing_registry PASSED
tests/test_model_availability_display.py::test_chats_page_includes_model_status_section PASSED
tests/test_model_availability_display.py::test_model_status_shows_friendly_messages PASSED
============================== 4 passed in 0.12s
```

## Usage Example

### Without Models Configured
When no models are configured or the model registry is unavailable, the chat interface displays normally without the model status panel. This ensures the UI remains functional even in environments where model management isn't set up.

### With Models Configured
When models are configured in the model registry:

1. Start the dashboard:
```bash
analyst dashboard
# or
python -m analyst_ledger.dashboard
```

2. Navigate to the chat interface at `http://127.0.0.1:8788/chats`

3. The model availability panel appears showing messages like:
```
Available Models
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🟢 Claude (Anthropic) (claude-sonnet-4) is in this Chat
🟢 Local open source (qwen3:8b) is in this Chat
```

## Benefits

1. **Transparency**: Users immediately see which AI models are available
2. **User-Friendly**: Clear, human-readable status messages instead of technical identifiers
3. **Context Awareness**: Helps users understand which models they're interacting with
4. **Graceful Degradation**: Works seamlessly whether models are configured or not
5. **Visual Feedback**: Green indicators provide quick visual confirmation of model availability

## Future Enhancements

Possible future improvements could include:
- Show model status (active/idle/busy)
- Display token usage or rate limits
- Add model switching controls
- Show model-specific capabilities
- Real-time status updates via JavaScript polling
