# Model Pipeline UI Mockups

Visual representation of proposed model management interfaces.

---

## 1. Current State (As-Is)

### Automation Edit Page - Model Selection

```
┌─────────────────────────────────────────────────────────┐
│ Edit automation                                          │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  Agent model                                             │
│  ┌────────────────────────────────────────────────┐    │
│  │ Choose before first run…                    ▼  │    │
│  └────────────────────────────────────────────────┘    │
│    • Claude — Anthropic API (or Bedrock via env)       │
│    • Qwen3 8B — OpenAI-compatible local/OS endpoint    │
│                                                          │
│  Pick Claude or Qwen3 8B before the first run.         │
│                                                          │
└─────────────────────────────────────────────────────────┘

PROBLEMS:
❌ Only 2 hardcoded options
❌ No way to add new models
❌ No visibility into model details
❌ No connection testing
```

---

## 2. Proposed State (To-Be)

### A. New Models Management Page (`/models`)

```
┌───────────────────────────────────────────────────────────────────────┐
│ Analyst Ledger                                                        │
├───────────────────────────────────────────────────────────────────────┤
│ Timeline  Sessions  Automations  [Models]  Chats  Settings          │
└───────────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────────┐
│ Models                                                                 │
├───────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  Manage agent models for workflow automations                        │
│                                                                        │
│  [+ Add Model]  [Import Config]  [Export All]                        │
│                                                                        │
├───────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 🟢 Claude 3.5 Sonnet                        [Edit] [Test] [🗑️]│  │
│  │ Anthropic API • Used in 12 automations                        │  │
│  │ Last tested: 2 hours ago ✓                                    │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 🟢 Qwen3 8B (Local)                         [Edit] [Test] [🗑️]│  │
│  │ Ollama • Used in 3 automations                                │  │
│  │ Last tested: 1 day ago ✓                                      │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ 🟢 GPT-4 Turbo                              [Edit] [Test] [🗑️]│  │
│  │ OpenAI API • Used in 0 automations                            │  │
│  │ Last tested: Never                                            │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ ⚫ Gemini Pro (Disabled)                    [Edit] [Test] [🗑️]│  │
│  │ Google AI • Used in 0 automations                             │  │
│  │ Last tested: 3 days ago ⚠ Connection failed                  │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                        │
└───────────────────────────────────────────────────────────────────────┘
```

### B. Add Model Form (`/models/add`)

```
┌───────────────────────────────────────────────────────────────────────┐
│ Add Model                                                              │
├───────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  [← Back to Models]                                                   │
│                                                                        │
│  ┌─ Quick Start ──────────────────────────────────────────────────┐  │
│  │ Start with a template:                                         │  │
│  │                                                                 │  │
│  │  [OpenAI]  [Anthropic]  [Google]  [Ollama]  [Custom]         │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  ┌─ Model Details ────────────────────────────────────────────────┐  │
│  │                                                                 │  │
│  │  Display Name *                                                │  │
│  │  ┌───────────────────────────────────────────────────────────┐│  │
│  │  │ GPT-4 Turbo                                                ││  │
│  │  └───────────────────────────────────────────────────────────┘│  │
│  │                                                                 │  │
│  │  Provider                                                      │  │
│  │  ┌───────────────────────────────────────────────────────────┐│  │
│  │  │ OpenAI                                                   ▼ ││  │
│  │  └───────────────────────────────────────────────────────────┘│  │
│  │                                                                 │  │
│  │  Model ID *                                                    │  │
│  │  ┌───────────────────────────────────────────────────────────┐│  │
│  │  │ gpt-4-turbo-preview                                        ││  │
│  │  └───────────────────────────────────────────────────────────┘│  │
│  │  The identifier used by the API                               │  │
│  │                                                                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  ┌─ API Configuration ────────────────────────────────────────────┐  │
│  │                                                                 │  │
│  │  API Endpoint *                                                │  │
│  │  ┌───────────────────────────────────────────────────────────┐│  │
│  │  │ https://api.openai.com/v1/chat/completions                ││  │
│  │  └───────────────────────────────────────────────────────────┘│  │
│  │                                                                 │  │
│  │  API Key                                                       │  │
│  │  ( ) Use environment variable: OPENAI_API_KEY                 │  │
│  │  (•) Enter key directly                                        │  │
│  │  ┌───────────────────────────────────────────────────────────┐│  │
│  │  │ sk-proj-••••••••••••••••••••••••••••••          [👁️]     ││  │
│  │  └───────────────────────────────────────────────────────────┘│  │
│  │  ⚠️ Keys are encrypted and stored securely                    │  │
│  │                                                                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  ┌─ Model Parameters ─────────────────────────────────────────────┐  │
│  │                                                                 │  │
│  │  Temperature                        Max Tokens                 │  │
│  │  ┌──────────────────────┐          ┌──────────────────────┐  │  │
│  │  │ 0.7    [━━━━━━━━━━○━━] 2.0      │ 4000                  │  │  │
│  │  └──────────────────────┘          └──────────────────────┘  │  │
│  │                                                                 │  │
│  │  Top P                              Frequency Penalty          │  │
│  │  ┌──────────────────────┐          ┌──────────────────────┐  │  │
│  │  │ 1.0    [━━━━━━━━━━━○] 1.0       │ 0.0                   │  │  │
│  │  └──────────────────────┘          └──────────────────────┘  │  │
│  │                                                                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  ┌─ Advanced Options ─────────────────────────────────────────────┐  │
│  │                                                                 │  │
│  │  [✓] Enable this model                                         │  │
│  │  [ ] Set as default model                                      │  │
│  │  [ ] Allow use for confidential data (local only)             │  │
│  │                                                                 │  │
│  │  Timeout (seconds)                                             │  │
│  │  ┌───────────────────────────────────────────────────────────┐│  │
│  │  │ 60                                                          ││  │
│  │  └───────────────────────────────────────────────────────────┘│  │
│  │                                                                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                        │
│  [Test Connection]    [Cancel]    [Save Model]                       │
│                                                                        │
└───────────────────────────────────────────────────────────────────────┘
```

### C. Enhanced Automation Edit Page - Model Selection

```
┌─────────────────────────────────────────────────────────────────────┐
│ Edit automation                                                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  Agent model                                                         │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ 🟢 Claude 3.5 Sonnet                                       ▼ │  │
│  └──────────────────────────────────────────────────────────────┘  │
│    • Claude 3.5 Sonnet (Anthropic API)                             │
│    • GPT-4 Turbo (OpenAI API)                                      │
│    • Qwen3 8B (Ollama - Local)                                     │
│    • Gemini Pro (Google AI) ⚠️ Connection failed                   │
│    ────────────────────────────────────────────                    │
│    • [+ Add New Model...]                                          │
│                                                                      │
│  Using Claude 3.5 Sonnet for research runs. Change anytime.        │
│  [Manage Models] [Test Connection]                                 │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘

IMPROVEMENTS:
✅ Shows all available models
✅ Status indicators (🟢 working, ⚠️ issues)
✅ Quick access to add new models
✅ Test connection button
✅ Link to full model management
```

### D. Connection Test Modal

```
┌───────────────────────────────────────────────────────────────┐
│ Testing Connection to GPT-4 Turbo                            │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  [●●●●●●○○○○] Testing...                                     │
│                                                               │
│  ✓ DNS resolution successful                                 │
│  ✓ TLS handshake successful                                  │
│  ✓ Authentication successful                                 │
│  ● Sending test prompt...                                    │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ Test Prompt:                                            │ │
│  │ "Hello, respond with 'OK' if you can read this."       │ │
│  │                                                          │ │
│  │ Response:                                                │ │
│  │ "OK"                                                     │ │
│  │                                                          │ │
│  │ Latency: 342ms                                           │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                               │
│  [Close]                                                      │
│                                                               │
└───────────────────────────────────────────────────────────────┘

SUCCESS STATE:
┌───────────────────────────────────────────────────────────────┐
│ Connection Successful ✓                                       │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  GPT-4 Turbo is ready to use.                                │
│                                                               │
│  • Latency: 342ms (Excellent)                                │
│  • Model version: gpt-4-turbo-2024-04-09                     │
│  • Max tokens: 128,000                                       │
│                                                               │
│  [Close]  [Save and Enable Model]                           │
│                                                               │
└───────────────────────────────────────────────────────────────┘

ERROR STATE:
┌───────────────────────────────────────────────────────────────┐
│ Connection Failed ✗                                           │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  ⚠️ Could not connect to GPT-4 Turbo                         │
│                                                               │
│  Error: Authentication failed                                │
│  Code: 401 Unauthorized                                      │
│                                                               │
│  Common solutions:                                           │
│  • Check that your API key is correct                       │
│  • Verify the key has not expired                           │
│  • Ensure you have sufficient credits                       │
│                                                               │
│  [Retry]  [Edit API Key]  [Close]                          │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

---

## 3. Mobile-Responsive Design

### Models List (Mobile)

```
┌─────────────────────────────┐
│ ☰  Models             [+]  │
├─────────────────────────────┤
│                             │
│ ┌─────────────────────────┐ │
│ │ 🟢 Claude 3.5 Sonnet    │ │
│ │ Anthropic API           │ │
│ │ 12 automations          │ │
│ │ [Edit] [Test]          │ │
│ └─────────────────────────┘ │
│                             │
│ ┌─────────────────────────┐ │
│ │ 🟢 Qwen3 8B             │ │
│ │ Ollama (Local)          │ │
│ │ 3 automations           │ │
│ │ [Edit] [Test]          │ │
│ └─────────────────────────┘ │
│                             │
│ ┌─────────────────────────┐ │
│ │ 🟢 GPT-4 Turbo          │ │
│ │ OpenAI API              │ │
│ │ 0 automations           │ │
│ │ [Edit] [Test]          │ │
│ └─────────────────────────┘ │
│                             │
└─────────────────────────────┘
```

---

## 4. Model Templates/Presets

### Template Selection

```
┌───────────────────────────────────────────────────────────────────┐
│ Choose a Model Template                                           │
├───────────────────────────────────────────────────────────────────┤
│                                                                    │
│  Popular Models                                                   │
│                                                                    │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐ │
│  │  OpenAI    │  │ Anthropic  │  │   Google   │  │   Ollama   │ │
│  │            │  │            │  │            │  │            │ │
│  │  GPT-4     │  │   Claude   │  │   Gemini   │  │   Llama    │ │
│  │  Turbo     │  │ 3.5 Sonnet │  │    Pro     │  │     3      │ │
│  │            │  │            │  │            │  │            │ │
│  │  [Select]  │  │  [Select]  │  │  [Select]  │  │  [Select]  │ │
│  └────────────┘  └────────────┘  └────────────┘  └────────────┘ │
│                                                                    │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐ │
│  │   Azure    │  │    AWS     │  │  Together  │  │   Custom   │ │
│  │            │  │            │  │     AI     │  │            │ │
│  │  OpenAI    │  │  Bedrock   │  │            │  │  Configure │ │
│  │  Service   │  │   Models   │  │  Open LLMs │  │   Manually │ │
│  │            │  │            │  │            │  │            │ │
│  │  [Select]  │  │  [Select]  │  │  [Select]  │  │  [Select]  │ │
│  └────────────┘  └────────────┘  └────────────┘  └────────────┘ │
│                                                                    │
│  [Cancel]                                                         │
│                                                                    │
└───────────────────────────────────────────────────────────────────┘
```

---

## 5. Import/Export Configuration

### Export Models

```
┌───────────────────────────────────────────────────────────────┐
│ Export Model Configurations                                   │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  Select models to export:                                    │
│                                                               │
│  [✓] Claude 3.5 Sonnet                                       │
│  [✓] Qwen3 8B (Local)                                        │
│  [✓] GPT-4 Turbo                                             │
│  [ ] Gemini Pro (Disabled)                                   │
│                                                               │
│  Export options:                                             │
│  [✓] Include API keys (encrypted)                           │
│  [✓] Include parameters                                      │
│  [ ] Include usage statistics                               │
│                                                               │
│  Format:  (•) JSON  ( ) YAML  ( ) TOML                      │
│                                                               │
│  [Cancel]  [Download Export]                                │
│                                                               │
└───────────────────────────────────────────────────────────────┘

EXPORTED JSON EXAMPLE:
{
  "version": "1.0",
  "exported_at": "2026-07-21T02:00:00Z",
  "models": [
    {
      "id": "gpt-4-turbo",
      "name": "GPT-4 Turbo",
      "provider": "openai",
      "endpoint": "https://api.openai.com/v1/chat/completions",
      "model_id": "gpt-4-turbo-preview",
      "api_key_env": "OPENAI_API_KEY",
      "parameters": {
        "temperature": 0.7,
        "max_tokens": 4000
      },
      "enabled": true
    }
  ]
}
```

---

## 6. Keyboard Shortcuts

```
Global Shortcuts:
  M         → Go to Models page
  Shift+M   → Add new model
  
Models Page:
  A         → Add new model
  /         → Search models
  E         → Edit selected model
  T         → Test selected model
  D         → Delete selected model
  Esc       → Cancel/close modal
  
Add Model Form:
  Ctrl+S    → Save model
  Ctrl+T    → Test connection
  Esc       → Cancel
```

---

## Summary of UI Improvements

### Key Features:
1. ✅ **Dedicated Models Page** - Central location for model management
2. ✅ **Visual Model Status** - Green/red indicators, last test time
3. ✅ **Template System** - Quick setup with pre-configured templates
4. ✅ **Connection Testing** - Validate models before saving
5. ✅ **Parameter Controls** - Visual sliders and inputs for tuning
6. ✅ **Import/Export** - Share configs across installations
7. ✅ **Mobile Responsive** - Works on all screen sizes
8. ✅ **Inline Model Management** - Add models without leaving automation edit
9. ✅ **Security Indicators** - Clear warnings about key storage
10. ✅ **Usage Tracking** - See which automations use each model

### Design Principles:
- **Progressive disclosure**: Show simple options first, advanced under toggle
- **Clear feedback**: Always show status, errors, and success messages
- **Keyboard accessible**: All actions available via keyboard
- **Consistent with existing UI**: Uses same design system as dashboard
- **Help text**: Inline hints explain each field
