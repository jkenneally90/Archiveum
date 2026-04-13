# Public Mode Administration Guide

A comprehensive guide for configuring, managing, and deploying Archiveum in Public Mode.

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [Configuration](#configuration)
4. [Admin Password Management](#admin-password-management)
5. [Switching Between Modes](#switching-between-modes)
6. [Session Management](#session-management)
7. [Security Considerations](#security-considerations)
8. [Troubleshooting](#troubleshooting)
9. [API Reference](#api-reference)

---

## Overview

Public Mode transforms Archiveum into a **locked, user-facing AI assistant interface** ideal for:

- **Company helpdesks** - Deploy a knowledge-base assistant for employees
- **Public information kiosks** - Library, museum, or visitor center interfaces
- **Shared workstations** - Single persona, multiple isolated users
- **Customer support portals** - Branded AI assistant for clients

### Key Features

| Feature | Description |
|---------|-------------|
| **Fixed Persona** | One pre-configured persona (e.g., "Alice") for all users |
| **Session Isolation** | Each user has isolated chat history (no cross-access) |
| **Simplified UI** | Chat-only interface with minimal controls |
| **Admin Access** | Hidden entry point for configuration changes |
| **Persistent Mode** | Setting survives server restarts |
| **Secure Sessions** | Cryptographically secure session tokens with expiry |

### What Users See vs. What Admins See

| Element | Public Mode User | Admin Mode |
|---------|------------------|------------|
| Navigation | Home link only | Full nav (Setup, Admin, Library, etc.) |
| Sidebar | Hidden | Visible with file upload, status, etc. |
| Persona Selector | Hidden | Visible and changeable |
| Chat History | Session-only | Global across all chats |
| Clear Chat Button | ✅ Yes (session only) | ✅ Yes (global) |
| Voice Controls | ✅ Yes | ✅ Yes |
| Interrupt Button | ✅ Yes | ✅ Yes |
| Avatar Panel | Hidden | Visible |

---

## Quick Start

### Step 1: Set Admin Password (Required First)

**Option A: Via Web UI (Recommended)**

1. Start Archiveum normally (default Admin Mode)
2. Navigate to **Admin** → **Settings**
3. Scroll to "Admin Password" section
4. Enter and confirm your password
5. Click **Save Settings**

**Option B: Via Configuration File**

Edit `archiveum_settings.json`:

```json
{
  "admin_password_hash": "your_hashed_password_here"
}
```

> **Note:** The hash is generated automatically when using the web UI. Manual hashing requires the `/admin/set-password` endpoint.

### Step 2: Configure Public Mode Persona

Before enabling Public Mode, configure your desired persona:

1. Go to **Admin** → **Persona**
2. Select or create the persona you want public users to interact with
3. Upload avatars and configure voice settings
4. Note the persona ID (e.g., "nova", "alice", "company_assistant")

### Step 3: Enable Public Mode

**Via Web UI:**

1. Navigate to **Admin** → **Settings**
2. Check **Enable Public Mode**
3. Set **Public Mode Persona ID** (e.g., "alice")
4. Adjust **Session Timeout** if needed (default: 30 minutes)
5. Save settings
6. Restart Archiveum

**Via Configuration File:**

Edit `archiveum_settings.json`:

```json
{
  "public_mode": true,
  "public_mode_persona_id": "alice",
  "admin_password_hash": "salt:hash...",
  "session_timeout_minutes": 30
}
```

Then restart Archiveum.

### Step 4: Verify Public Mode

1. Open Archiveum in an incognito/private browser window
2. You should see:
   - Only "Home" in navigation
   - No sidebar with file uploads
   - No persona selector
   - Chat interface only
   - Clear Chat button
3. The persona should respond according to your configured public persona

---

## Configuration

### Configuration Options

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `public_mode` | boolean | `false` | Enable/disable Public Mode |
| `public_mode_persona_id` | string | `"nova"` | Persona ID used in Public Mode |
| `admin_password_hash` | string | `""` | Hashed admin password (PBKDF2-SHA256) |
| `session_timeout_minutes` | integer | `30` | Session expiry after inactivity |

### Environment Variables

You can also set these via environment variables:

```bash
export ARCHIVEUM_PUBLIC_MODE=true
export ARCHIVEUM_PUBLIC_MODE_PERSONA_ID=alice
export ARCHIVEUM_ADMIN_PASSWORD_HASH="..."
export ARCHIVEUM_SESSION_TIMEOUT_MINUTES=30
```

Environment variables take precedence over the settings file.

### Complete Example Configuration

```json
{
  "host": "0.0.0.0",
  "port": 8000,
  "public_mode": true,
  "public_mode_persona_id": "company_assistant",
  "admin_password_hash": "a1b2c3d4...:e5f6g7h8...",
  "session_timeout_minutes": 60,
  "enable_voice": true,
  "speak_responses": true,
  "current_persona_id": "admin_persona",
  "ollama_chat_model": "llama3.1:8b",
  "custom_personas": [...]
}
```

---

## Admin Password Management

### Setting/Changing Password

**Via Web UI:**

1. In Admin Mode, go to **Admin** → **Settings**
2. Enter current password (if set)
3. Enter new password and confirm
4. Save

**Via API:**

```bash
curl -X POST http://localhost:8000/admin/set-password \
  -d "current_password=oldpass" \
  -d "new_password=newpass" \
  -d "redirect_to=/"
```

### Password Requirements

- Minimum 8 characters recommended
- Can include any characters
- Stored using PBKDF2-SHA256 with 100,000 iterations
- Unique salt per password

### Lost Password Recovery

If you lose the admin password:

1. Stop Archiveum
2. Edit `archiveum_settings.json`
3. Set `"admin_password_hash": ""`
4. Restart Archiveum
5. Access Admin Mode and set a new password

---

## Switching Between Modes

### Admin → Public Mode

**Via Web UI:**

1. Access Admin Mode (if in Public Mode, see "Accessing Admin from Public Mode" below)
2. Go to **Admin** → **Settings**
3. Check **Enable Public Mode**
4. Save and restart

**Via API:**

```bash
curl -X POST http://localhost:8000/mode/switch \
  -d "public_mode=true" \
  -d "admin_password=yourpassword" \
  -d "redirect_to=/"
```

### Public → Admin Mode

**Via Hidden Admin Access:**

1. In Public Mode, click the **bottom-right corner** 5 times rapidly
2. Admin password modal appears
3. Enter password
4. Click **Access Admin**
5. You now have 1-hour admin access

**Via API (if you have shell access):**

```bash
curl -X POST http://localhost:8000/mode/switch \
  -d "public_mode=false" \
  -d "admin_password=yourpassword" \
  -d "redirect_to=/"
```

### Temporary Admin Access

When you access admin from Public Mode, a cookie grants temporary access:

- **Duration:** 1 hour
- **Scope:** Current browser session
- **Cookie:** `admin_access=granted`
- **Security:** HttpOnly, SameSite=strict

After expiry, the interface returns to Public Mode automatically.

---

## Session Management

### How Sessions Work

1. **Session Creation:**
   - New users get a session on first page load
   - Session ID stored in cookie (`session_id`)
   - Cookie expires after 24 hours

2. **Session Data:**
   - Chat history isolated per session
   - Temporary memory/context per session
   - Last activity tracked for expiry

3. **Session Expiry:**
   - Inactive sessions expire after timeout (default 30 min)
   - Browser close may clear session (depending on settings)
   - Expired sessions are cleaned up automatically

4. **Session Isolation:**
   - Users cannot access other sessions' data
   - Backend enforces strict boundary checks
   - No session ID guessing possible (32-byte random tokens)

### Monitoring Sessions

**View Session Statistics:**

```bash
curl http://localhost:8000/session/stats
```

Response:
```json
{
  "ok": true,
  "stats": {
    "active_sessions": 12,
    "session_timeout_minutes": 30,
    "storage_file": ".../archiveum_data/sessions.json"
  }
}
```

### Manual Session Cleanup

Sessions clean up automatically every 5 minutes. To force cleanup:

1. Restart Archiveum
2. Or wait for background cleanup cycle

### Session Storage

Sessions are stored in:
- File: `archiveum_data/sessions.json`
- Format: JSON with session_id as key
- Persistence: Survives server restarts (non-expired sessions)

---

## Security Considerations

### Session Security

| Feature | Implementation |
|---------|----------------|
| Session ID Generation | `secrets.token_urlsafe(32)` - cryptographically secure |
| Cookie Attributes | HttpOnly, SameSite=strict |
| Session Expiry | Time-based + activity tracking |
| Cross-Session Access | Enforced server-side, no trust of client |
| Session Storage | Server-side only, not in client |

### Admin Access Security

| Feature | Implementation |
|---------|----------------|
| Password Hashing | PBKDF2-SHA256, 100k iterations |
| Salt | Unique random 32-byte salt per password |
| Comparison | `secrets.compare_digest()` (constant-time) |
| Brute Force | No built-in rate limiting (use reverse proxy) |

### Recommended Security Practices

1. **Use HTTPS** in production
   - Public Mode transmits session cookies
   - Admin passwords should never be sent over HTTP

2. **Set Strong Admin Password**
   - Minimum 12 characters
   - Mix of letters, numbers, symbols
   - Unique to Archiveum

3. **Configure Reverse Proxy**
   - Add rate limiting for `/mode/admin-access`
   - Add fail2ban for repeated failures
   - Example nginx config:

```nginx
limit_req_zone $binary_remote_addr zone=admin:10m rate=5r/m;

location /mode/admin-access {
    limit_req zone=admin burst=3 nodelay;
    proxy_pass http://archiveum;
}
```

4. **Session Timeout Tuning**
   - Public kiosks: 5-10 minutes
   - Office workstations: 30-60 minutes
   - Secure environments: 2-5 minutes

5. **Regular Auditing**
   - Check `/session/stats` for unusual activity
   - Review session file size
   - Monitor for expired session buildup

### Data Privacy

**What's Isolated Per Session:**
- Chat messages
- User inputs
- Assistant responses
- Temporary conversation context

**What's Shared Globally:**
- Persona configuration
- System prompts
- Indexed documents/knowledge base
- Voice settings
- Avatar media

**No Personal Data Stored:**
- No user names, emails, or identifiers
- Sessions are anonymous by design
- IP addresses not logged in sessions

---

## Troubleshooting

### Issue: Can't Access Admin Mode

**Symptoms:** Clicking corner 5 times does nothing

**Solutions:**
1. Ensure admin password is set (check settings)
2. Click rapidly within 2 seconds
3. Look for small trigger area (20x20px bottom-right)
4. Check browser console for JavaScript errors
5. Try accessing directly: POST to `/mode/admin-access`

### Issue: Sessions Not Isolating

**Symptoms:** Users see each other's chats

**Solutions:**
1. Verify `public_mode: true` in settings
2. Check session cookie is being set
3. Ensure browser allows cookies
4. Check session file permissions
5. Review session_manager.py logs

### Issue: Sessions Expiring Too Fast

**Symptoms:** Users lose chats after short time

**Solutions:**
1. Increase `session_timeout_minutes` in settings
2. Check system clock is accurate
3. Verify no cleanup script running externally
4. Check for session file corruption

### Issue: Password Not Working

**Symptoms:** "Invalid password" error

**Solutions:**
1. Check caps lock
2. Reset password via settings file (set hash to "")
3. Ensure password meets minimum length
4. Try setting password fresh via web UI

### Issue: Mode Not Persisting

**Symptoms:** Reverts to Admin Mode after restart

**Solutions:**
1. Verify settings file is writable
2. Check `persist_settings` is working
3. Look for errors in Archiveum logs
4. Manually verify JSON in settings file

### Debug Mode

Enable verbose logging:

```python
# In archiveum_settings.json or environment
"log_level": "DEBUG"
```

Check logs for:
- Session creation events
- Mode switch events
- Cookie handling
- Session validation failures

---

## API Reference

### Session Endpoints

#### GET `/session/init`
Create a new session manually.

**Response:**
```json
{
  "ok": true,
  "session_id": "abc123...",
  "message": "Session created"
}
```

**Sets Cookie:** `session_id=<token>`

---

#### GET `/session/stats`
Get session manager statistics (admin only).

**Response:**
```json
{
  "ok": true,
  "stats": {
    "active_sessions": 12,
    "session_timeout_minutes": 30,
    "storage_file": "..."
  }
}
```

---

### Mode Endpoints

#### GET `/mode/status`
Check current mode configuration.

**Response:**
```json
{
  "ok": true,
  "public_mode": true,
  "public_mode_persona_id": "alice",
  "current_persona_id": "admin_persona",
  "has_admin_password": true,
  "session_timeout_minutes": 30
}
```

---

#### POST `/mode/admin-access`
Gain temporary admin access from Public Mode.

**Parameters:**
- `password` (string, required): Admin password
- `redirect_to` (string, optional): Where to redirect after

**Success:** Sets `admin_access` cookie, redirects

**Failure:** Redirects with error message

---

#### POST `/mode/switch`
Switch between Public and Admin modes.

**Parameters:**
- `public_mode` (string, required): "true" or "false"
- `admin_password` (string, required if switching to admin): Current password
- `redirect_to` (string, optional): Where to redirect

**Example:**
```bash
curl -X POST http://localhost:8000/mode/switch \
  -d "public_mode=true" \
  -d "admin_password=mypassword"
```

---

#### POST `/admin/set-password`
Set or change admin password.

**Parameters:**
- `current_password` (string, required if already set): Current password
- `new_password` (string, required): New password to set
- `redirect_to` (string, optional): Where to redirect

**Example:**
```bash
# First time setting password
curl -X POST http://localhost:8000/admin/set-password \
  -d "new_password=newpass123"

# Changing existing password
curl -X POST http://localhost:8000/admin/set-password \
  -d "current_password=oldpass" \
  -d "new_password=newpass123"
```

---

### Chat Endpoints (Session-Aware)

#### POST `/chat`
Submit a chat message. Automatically handles session isolation.

**Parameters:**
- `question` (string, required): User's question
- `session_id` (string, optional but recommended): Session ID for Public Mode

**Behavior:**
- In Public Mode: Stores chat in session-specific history
- In Admin Mode: Stores in global chat history

---

#### POST `/chat/session/clear`
Clear chat history for current session only.

**Parameters:**
- `session_id` (string, required): Session ID
- `redirect_to` (string, optional): Where to redirect

**Behavior:** Only clears the specified session's history, not other sessions.

---

## Deployment Scenarios

### Scenario 1: Company Helpdesk

**Setup:**
- Persona: "IT_Helpdesk" with company knowledge
- Timeout: 30 minutes
- Voice: Enabled for accessibility

**Configuration:**
```json
{
  "public_mode": true,
  "public_mode_persona_id": "it_helpdesk",
  "session_timeout_minutes": 30,
  "enable_voice": true,
  "speak_responses": true
}
```

**Access:**
- Employees use the public interface
- IT admin uses corner-click + password for maintenance

---

### Scenario 2: Museum Information Kiosk

**Setup:**
- Persona: "Museum_Guide" with exhibit information
- Timeout: 10 minutes (quick turnover)
- Voice: Enabled
- Large text mode via custom CSS

**Configuration:**
```json
{
  "public_mode": true,
  "public_mode_persona_id": "museum_guide",
  "session_timeout_minutes": 10,
  "enable_voice": true
}
```

---

### Scenario 3: Library Research Assistant

**Setup:**
- Persona: "Research_Librarian"
- Timeout: 60 minutes (long research sessions)
- Extensive document indexing
- No voice (quiet environment)

**Configuration:**
```json
{
  "public_mode": true,
  "public_mode_persona_id": "research_librarian",
  "session_timeout_minutes": 60,
  "enable_voice": false,
  "speak_responses": false
}
```

---

## Best Practices

### Before Enabling Public Mode

1. ✅ Thoroughly test persona in Admin Mode
2. ✅ Set strong admin password
3. ✅ Configure appropriate session timeout
4. ✅ Index all necessary documents
5. ✅ Test voice features (if enabled)
6. ✅ Upload and tag all persona avatars
7. ✅ Test interrupt button functionality
8. ✅ Review security settings

### Maintenance

**Weekly:**
- Check session stats for anomalies
- Review chat history size
- Monitor disk usage

**Monthly:**
- Update admin password
- Review and prune old sessions
- Check for software updates
- Backup settings file

**As Needed:**
- Add new documents to knowledge base
- Update persona instructions
- Adjust session timeout based on usage

### Performance Tuning

**High Traffic Sites:**
- Reduce session timeout (5-10 min)
- Enable automatic session cleanup
- Use SSD for session storage
- Consider reverse proxy caching

**Low Traffic Sites:**
- Increase session timeout (60+ min)
- More generous resource allocation
- Detailed logging for debugging

---

## Migration Guide

### From Single-User to Public Mode

1. Document current persona settings
2. Export chat history (if needed)
3. Set admin password
4. Configure Public Mode settings
5. Restart Archiveum
6. Test thoroughly in incognito window
7. Deploy to users

### From Public Mode Back to Single-User

1. Access admin mode (corner-click + password)
2. Go to Settings
3. Uncheck "Enable Public Mode"
4. Save and restart
5. All session data is preserved but not used

---

## Support

For issues not covered in this guide:

1. Check Archiveum logs for error messages
2. Review session file integrity
3. Verify settings file permissions
4. Test with fresh browser/incognito mode
5. Consult the main Archiveum documentation

---

## Changelog

### Version 1.0 (Initial Public Mode)
- Session isolation with secure tokens
- Admin password protection
- Mode switching with persistence
- Hidden admin access entry point
- Automatic session expiry
- Session-specific chat clearing

---

*This guide is maintained alongside Archiveum releases. For the latest updates, check the repository documentation.*
