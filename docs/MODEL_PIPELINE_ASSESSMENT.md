# Model Pipeline Assessment
**Assessment Date:** July 21, 2026  
**Reviewed By:** Cloud Agent  
**Scope:** Pipeline for adding open source and frontier model APIs

---

## Executive Summary

**Current State:** ❌ **No pipeline exists**  
**Ease of Use Rating:** 1/10 (Hardcoded only)  
**Improvement Potential:** ⭐⭐⭐⭐⭐ (Significant room for improvement)

The Analyst Ledger application currently has **NO user-facing pipeline** for adding open source or frontier model APIs. Models are hardcoded in the source code and require developer intervention to add new models.

---

## Current Implementation Analysis

### 1. **Model Configuration Location**

Models are hardcoded in `src/analyst_ledger/models.py`:

```python
AGENT_MODELS: Dict[str, Dict[str, str]] = {
    "claude": {
        "id": "claude",
        "label": "Claude",
        "destination": "anthropic",
        "description": "Anthropic API (or Bedrock via env)",
    },
    "qwen3-8b": {
        "id": "qwen3-8b",
        "label": "Qwen3 8B",
        "destination": "qwen",
        "description": "OpenAI-compatible local/OS endpoint (Ollama, vLLM, MLX, …)",
    },
}
```

### 2. **Dashboard UI**

The dashboard at `http://127.0.0.1:8788/automations/<ritual_id>` provides:
- A dropdown menu with only **2 models** (Claude and Qwen3 8B)
- No "Add Model" button or configuration interface
- No way to customize model endpoints, API keys, or parameters

**HTML Output:**
```html
<select id="agent-model">
  <option value="">Choose before first run…</option>
  <option value="claude">Claude — Anthropic API (or Bedrock via env)</option>
  <option value="qwen3-8b">Qwen3 8B — OpenAI-compatible local/OS endpoint</option>
</select>
```

### 3. **Model Destinations**

The system currently supports two model destinations (hardcoded in `orchestration.py`):
1. **anthropic** - Calls Anthropic API via `_call_anthropic_messages()`
2. **qwen** - Calls OpenAI-compatible endpoints via `_call_openai_compatible_messages()`

### 4. **Configuration Method**

Models are configured exclusively via **environment variables**:
- `ANTHROPIC_API_KEY` - For Claude
- `ANALYST_QWEN_BASE_URL` - For Qwen endpoint
- `ANALYST_QWEN_MODEL` - For Qwen model name
- `ANALYST_QWEN_API_KEY` - Optional for Qwen

---

## Ease of Use Assessment

### Rating: **1/10** 

#### Breakdown:

| Aspect | Rating | Notes |
|--------|--------|-------|
| **Discovery** | 1/10 | No visible UI to add models; requires code reading |
| **Configuration** | 2/10 | Must edit source code or environment variables |
| **User Experience** | 1/10 | No self-service capability for non-developers |
| **Flexibility** | 3/10 | Limited to 2 model types, no custom endpoints |
| **Documentation** | 5/10 | README explains existing models but not how to add new ones |

### User Journey Problems:

1. **No Discovery**: Users cannot find where to add new models
2. **Developer Barrier**: Adding models requires:
   - Editing Python source code
   - Understanding the codebase structure
   - Modifying `models.py` and potentially `orchestration.py`
   - Reinstalling the package
3. **No Validation**: No UI to test model connections before saving
4. **No Management**: Cannot disable, edit, or remove models via UI

---

## Comparison to Industry Standards

### What Users Expect (Based on Modern AI Tools):

#### ✅ Good Examples:
- **LangChain Studio**: Visual model selector with "Add Custom Model" button
- **OpenAI Playground**: Easy API key input with instant validation
- **Ollama UI**: Drag-and-drop model installation
- **Jan.ai**: Browse model library with one-click installs

#### ❌ Current Implementation:
- No UI for adding models
- No model library or marketplace
- No connection testing
- No model parameter configuration (temperature, max tokens, etc.)

---

## Improvement Recommendations

### Priority 1: **Immediate Wins** (High Impact, Low Effort)

#### 1.1 Add UI for Model Management
Create a `/models` page in the dashboard with:
- **List View**: Show all available models
- **Add Model Form**: 
  ```
  [ ] Model Type: [OpenAI Compatible ▼]
  [ ] Display Name: _____________
  [ ] API Endpoint: _____________
  [ ] API Key: ****************** [Test Connection]
  [ ] Model ID: _____________
  ```
- **Test Connection**: Validate credentials before saving
- **Edit/Delete**: Manage existing models

#### 1.2 Persist Models in Database
Store model configurations in:
- SQLite table `agent_models` (already have `ledger.sqlite3`)
- Fields: `id`, `name`, `type`, `endpoint`, `api_key_ref`, `parameters`, `enabled`
- Fallback to hardcoded models if database is empty

#### 1.3 Environment Variable Support (Keep Backwards Compatibility)
- Continue supporting `ANTHROPIC_API_KEY`, etc.
- Add `ANALYST_CUSTOM_MODELS` JSON environment variable for power users

### Priority 2: **Enhanced Features** (High Impact, Medium Effort)

#### 2.1 Model Templates
Pre-configured templates for common models:
- OpenAI (GPT-4, GPT-3.5)
- Anthropic (Claude 3.5 Sonnet, Claude 3 Opus)
- Google (Gemini Pro, Gemini Ultra)
- Ollama (Llama 3, Mistral, Qwen)
- Azure OpenAI
- AWS Bedrock

#### 2.2 Model Parameters UI
Allow customization per model:
- Temperature (0.0 - 2.0)
- Max tokens
- Top P
- Frequency penalty
- Presence penalty

#### 2.3 Model Usage Tracking
- Track API calls per model
- Show costs (if pricing data available)
- Rate limiting per model

### Priority 3: **Advanced Features** (High Impact, High Effort)

#### 3.1 Model Marketplace
- Browse community-contributed model configs
- One-click install from catalog
- Version control for model configs

#### 3.2 Model Fallbacks
- Primary/secondary model configuration
- Automatic failover if primary fails
- Cost optimization (use cheaper model first)

#### 3.3 Model Comparison Tools
- Side-by-side model testing (already have arena mode!)
- Performance benchmarking
- Cost comparison

---

## Proposed Architecture

### Data Model:

```python
# New table: agent_models
{
    "id": "gpt-4-turbo",  # unique identifier
    "name": "GPT-4 Turbo",  # display name
    "provider": "openai",  # openai, anthropic, ollama, custom
    "type": "openai_compatible",  # api type
    "endpoint": "https://api.openai.com/v1/chat/completions",
    "api_key_env": "OPENAI_API_KEY",  # env var name for key
    "model_id": "gpt-4-turbo-preview",  # model identifier for API
    "parameters": {
        "temperature": 0.7,
        "max_tokens": 4000
    },
    "enabled": true,
    "created_at": "2026-07-21T00:00:00Z",
    "last_tested_at": "2026-07-21T02:00:00Z",
    "test_status": "success"  # success, failed, not_tested
}
```

### API Endpoints:

```
GET    /api/models              # List all models
POST   /api/models              # Add new model
PUT    /api/models/<id>         # Update model
DELETE /api/models/<id>         # Delete model
POST   /api/models/<id>/test    # Test model connection
```

### UI Pages:

```
/models                          # Model management dashboard
/models/add                      # Add new model form
/models/<id>/edit                # Edit existing model
```

---

## Implementation Roadmap

### Phase 1: Foundation (1-2 weeks)
- [ ] Create `agent_models` SQLite table
- [ ] Add model CRUD operations in `ledger.py`
- [ ] Create `/models` dashboard page
- [ ] Implement "Add Model" form with connection testing
- [ ] Update `models.py` to load from database + fallback to hardcoded

### Phase 2: Enhanced UX (1 week)
- [ ] Add model templates/presets
- [ ] Implement parameter customization UI
- [ ] Add model validation and error handling
- [ ] Create model selection UI improvements (search, filter, favorites)

### Phase 3: Advanced Features (2-3 weeks)
- [ ] Model usage tracking and analytics
- [ ] Cost estimation and tracking
- [ ] Model fallback/redundancy system
- [ ] Import/export model configurations

---

## Security Considerations

### Current Implementation:
✅ API keys stored in environment variables (not in code)  
⚠️ No encryption for stored credentials  
⚠️ No access control (anyone with dashboard access can see all models)

### Recommended:
1. **Encrypt API keys** at rest using OS keyring or encrypted SQLite extension
2. **Add authentication** to dashboard if not already present
3. **Audit logging** for model additions/changes
4. **Secrets management** integration (HashiCorp Vault, AWS Secrets Manager)
5. **Principle of least privilege** - separate read/write permissions for models

---

## Migration Path

To maintain backwards compatibility:

```python
def list_agent_models():
    """Load models from database, fallback to hardcoded"""
    models = _load_models_from_db()
    if not models:
        # Fallback to hardcoded for backwards compatibility
        models = _hardcoded_models()
    return models
```

This ensures existing users continue working without changes.

---

## Conclusion

### Current State Summary:
- **No pipeline exists** for adding models via UI
- Models are **hardcoded** in Python source files
- Adding models requires **developer intervention**
- **Ease of use: 1/10** for non-technical users

### Improvement Potential:
With the recommended changes, the system could achieve:
- **9/10 ease of use** with intuitive UI
- **Self-service model management** for all users
- **Industry-standard UX** comparable to modern AI tools
- **Significant time savings** (minutes vs. hours to add new models)

### ROI Estimate:
- **Developer time saved**: ~4 hours per model addition → ~5 minutes
- **User empowerment**: Non-developers can now manage models independently
- **Flexibility**: Rapid adaptation to new model releases
- **Cost optimization**: Easy experimentation with different providers

### Recommendation:
**Priority: HIGH** - Implementing a model management pipeline would significantly improve the product's usability and competitive position. Start with Phase 1 (Foundation) to deliver immediate value, then iterate based on user feedback.
