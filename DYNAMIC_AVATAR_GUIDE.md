# Dynamic Avatar System - Complete Guide

## Overview

The Archiveum Dynamic Avatar System enables your Persona to automatically switch avatars based on the emotional tone, context, and intent detected in user messages. This creates a more immersive and responsive conversational experience.

---

## The 19-Tag System

The system recognizes **19 distinct tags** organized into three categories:

### 1. Emotional Tones (11 tags)
Priority: **Highest** - These are detected first in conversation

| Tag | Description | Example Keywords |
|-----|-------------|------------------|
| `neutral` | Default, balanced state | (fallback when no emotion detected) |
| `happy` | Joyful, positive, delighted | love, great, wonderful, joy, smile |
| `excited` | Thrilled, energetic, enthusiastic | wow, incredible, thrilled, amazing |
| `sad` | Melancholy, sorrowful, grieving | sad, sorry, miss, tears, lonely |
| `angry` | Furious, irritated, frustrated | angry, furious, hate, rage, annoyed |
| `curious` | Inquisitive, interested, exploring | curious, question, how, why, learn |
| `playful` | Fun, humorous, lighthearted | play, joke, fun, silly, game |
| `serious` | Focused, grave, earnest | serious, important, critical, focus |
| `romantic` | Passionate, affectionate, loving | romantic, love, heart, passion, adore |
| `mysterious` | Enigmatic, cryptic, intriguing | mysterious, secret, unknown, puzzle |
| `calm` | Peaceful, serene, tranquil | calm, peaceful, relax, quiet, zen |

### 2. Contextual Topics (4 tags)
Priority: **Medium** - Detected when discussing specific subjects

| Tag | Description | Example Keywords |
|-----|-------------|------------------|
| `space` | Space, cosmos, astronomy | star, planet, galaxy, rocket, mars |
| `technology` | Tech, digital, computing | computer, software, ai, robot, code |
| `nature` | Natural world, outdoors | forest, tree, mountain, ocean, wildlife |
| `battle` | Combat, conflict, warfare | battle, fight, warrior, weapon, victory |

### 3. Temporal / Contextual (4 tags)
Priority: **Context-based** - Time of day or special occasions

| Tag | Description | Example Keywords |
|-----|-------------|------------------|
| `morning` | Dawn, early hours | morning, sunrise, breakfast, coffee |
| `afternoon` | Midday hours | afternoon, lunch, noon, work |
| `evening` | Night hours | evening, sunset, night, dinner, stars |
| `celebration` | Festive occasions | celebrate, party, birthday, wedding |

---

## How It Works

### 1. Message Analysis
When you send a message, the system:
1. Scans for keywords matching each tag
2. Scores matches (multiple keywords = higher score)
3. Returns the highest-scoring tag, or `neutral` if no matches

### 2. Avatar Selection
The system then:
1. Looks for avatars tagged with the detected emotion
2. If no match: falls back to `neutral` or `default` avatar
3. If still no match: uses the first available avatar

**Avatar Persistence**: When no emotion keywords are detected in a message (resulting in `neutral`), the system **retains the current avatar** instead of reverting to default. The avatar only changes when a new, different emotion is explicitly detected. This maintains visual consistency during conversations.

### 3. Debouncing
To prevent rapid avatar flickering:
- **5-second minimum** between avatar switches
- Same emotion = no switch needed
- Pending emotion queued for next allowed switch
- **Neutral detection = no change** (current avatar persists)

---

## Uploading Avatars

### Storage Location
```
data/avatars/{persona_id}/
├── nova_happy_a1b2c3d4.png
├── nova_happy_a1b2c3d4.png.json  (metadata)
├── nova_sad_x9y8z7w6.gif
├── nova_space_moon.jpg
└── ...
```

### File Requirements
- **Formats**: PNG, JPEG, GIF, WebP, MP4, WebM
- **Max per file**: 50 MB
- **Max per persona**: 500 MB
- **Dimensions**: Recommend 9:16 aspect ratio for best display

### Method 1: Web UI (Recommended)
1. Navigate to **Admin → Persona**
2. Find your persona and click **"Manage Emotional Avatars"**
3. Select a file, choose the tag, and upload
4. The file will be automatically renamed with tag and hash

### Method 2: Manual Upload
1. Copy files to `data/avatars/{persona_id}/`
2. Name format: `{persona_id}_{tag}_{hash}.{ext}`
  - Example: `nova_happy_a1b2c3d4.png`
3. Create optional `.json` sidecar for metadata:
```json
{
  "tags": ["happy"],
  "default": true,
  "original_filename": "my_avatar.png",
  "description": "A joyful expression in a sunlit garden, wearing a flowing blue dress with flowers in the background"
}
```

### Setting a Default Avatar
- Check **"Set as Default"** when uploading, OR
- Set `"default": true` in the sidecar JSON
- Used when no emotion is detected or no matching tag exists

---

## Avatar Descriptions for LLM Context

### Overview

When you provide **avatar descriptions**, the LLM becomes aware of the Persona's current visual state. This enables the AI to:

- Reference its appearance naturally in conversation
- Describe what it's "doing" or "wearing" 
- Comment on its surroundings or mood based on the visual
- Create more immersive, embodied interactions

### How It Works

1. **Upload avatar with description**: When adding media, describe the visual appearance
2. **Emotion detection triggers avatar switch**: User message → emotion tag → avatar selection
3. **LLM receives visual context**: The description is injected into the system prompt
4. **Persona responds with awareness**: References to appearance feel natural and contextual

### Description Examples

| Avatar Type | Good Description | Why It Works |
|-------------|------------------|--------------|
| **Happy** | "Smiling warmly in a sunlit garden, wearing a flowing summer dress, surrounded by blooming flowers" | Sets scene, clothing, mood |
| **Serious** | "Seated at a wooden desk in a study, wearing formal attire, soft lamp light, books visible in background" | Establishes setting and tone |
| **Space** | "Floating among stars with Earth visible behind, wearing a sleek silver spacesuit, cosmic nebula colors" | Creates immersive context |
| **Battle** | "In armor on a misty battlefield, determined stance, sunrise breaking through clouds behind" | Establishes action and atmosphere |

### Writing Effective Descriptions

**DO:**
- Describe setting/environment
- Mention clothing or distinctive features
- Include lighting/atmosphere
- Keep it 1-2 sentences (100-200 chars ideal)
- Make it evocative but natural

**DON'T:**
- Be overly detailed (LLM will get distracted)
- Use technical photography terms
- Write descriptions that force specific responses
- Make every avatar description drastically different (keeps continuity)

### Technical Implementation

**Storage**: Descriptions are saved in the `.json` sidecar file:
```json
{
  "tags": ["happy"],
  "default": false,
  "description": "Standing on a beach at sunset, casual linen clothes, warm golden light",
  "uploaded_at": "2024-01-15 14:32:10"
}
```

**Latency Impact**: Minimal (~0-2ms). The description is injected as text into the system prompt.

**LLM Context Format**:
```
[Current visual state]: You are currently displaying as: {description}
```

This appears in the prompt after memory context, allowing the LLM to reference its appearance naturally.

### Example Conversations

**With happy avatar** ("Smiling in a sunlit meadow, wildflowers all around, wearing a bright yellow sundress"):
- User: "What's your favorite season?"
- LLM: "Oh, I'd have to say spring — everything's coming to life, just like these wildflowers around me. It's hard not to feel cheerful with all this sunlight!"

**With space avatar** ("Floating in orbit above Earth, wearing a white spacesuit, stars infinite behind"):
- User: "Do you ever feel small?"
- LLM: "Sometimes, looking down at Earth from up here... it puts things in perspective. But in a good way — like we're all part of something vast and wonderful."

**Without avatar description**: The LLM responds normally but has no visual context to reference.

---

## Avatar Lightbox Viewer

### Overview

Click any avatar on the **Home Page** to open a **full-size lightbox modal**. This works for:
- **Images** (PNG, JPEG, GIF, WebP) — viewed at full resolution
- **Videos** (MP4, WebM) — played with full playback controls

### How to Use

1. **Navigate to the Home Page** (`/`)
2. **Hover over the avatar** in the "Current Persona" panel — a 🔍 icon appears
3. **Click the avatar** — the lightbox opens with full-size view
4. **Close the lightbox**:
   - Click the **X button** (top-right)
   - Click **outside the image** (on the dark background)
   - Press **ESC key**

### Video Controls

When viewing video avatars in the lightbox:
- **Play/Pause**: Click the video or use the control bar
- **Seek**: Drag the progress bar
- **Mute/Unmute**: Volume control (videos autoplay muted in thumbnail view)
- **Fullscreen**: Use the fullscreen button for maximum size
- **Loop**: Videos loop automatically

### Mobile Support

The lightbox is fully responsive:
- Portrait images/videos scale to fit screen height
- Swipe gestures work for navigation (future enhancement)
- Close button is oversized for touch targets

### Safety & Security

**Yes, this feature is safe** because:
- All media URLs are **sanitized and escaped** (`escape()` function)
- Only **same-origin** content from your Archiveum server is displayed
- No external URLs or user-generated links are accepted
- The modal prevents background scrolling but doesn't access sensitive data
- Videos use standard HTML5 `<video>` controls (no custom plugins)

---

## Testing the System

### Test Endpoints

1. **Test emotion detection**:
   ```
   GET /test/emotion
   ```
   Returns test results for sample messages.

2. **Analyze custom message**:
   ```
   GET /analyze/emotion?message=I love this so much!
   ```
   Returns detected emotion for any message.

3. **Get avatar for message**:
   ```
   GET /avatar/emotional?message=Tell me about space&persona_id=nova
   ```
   Returns the avatar HTML that would display.

---

## Expanding the System

### Adding New Keywords to Existing Tags

Edit `EMOTION_KEYWORDS` in `archiveum/webapp.py`:

```python
EMOTION_KEYWORDS = {
    "happy": [
        # existing keywords...
        "elated", "ecstatic", "blissful",  # add these
    ],
    # ... other tags
}
```

### Adding New Tags (Advanced)

**Step 1**: Add to `EMOTIONAL_TAGS`:
```python
EMOTIONAL_TAGS = {
    # existing tags...
    "surprised",  # add new tag
}
```

**Step 2**: Add keywords:
```python
EMOTION_KEYWORDS = {
    # existing keywords...
    "surprised": [
        "surprise", "shocked", "amazed", "unexpected",
        "wow", "whoa", "gasp", "astonished"
    ],
}
```

**Step 3**: Update UI form in `_render_media_upload_form()`:
Add the new tag to the appropriate `<optgroup>`.

**Step 4**: Restart Archiveum

### Using LLM-Based Detection (Future Enhancement)

Replace `_analyze_emotion_simple()` with LLM analysis:

```python
async def _analyze_emotion_llm(message: str) -> str:
    """Use LLM to detect emotion with nuanced understanding."""
    prompt = f"""
    Analyze this message and select the most appropriate tag from:
    {', '.join(EMOTIONAL_TAGS)}
    
    Message: {message}
    
    Return ONLY the tag name, nothing else.
    """
    response = await llm.complete(prompt)
    tag = response.strip().lower()
    return tag if tag in EMOTIONAL_TAGS else "neutral"
```

---

## Best Practices

### Avatar Naming Conventions
- Use descriptive base names: `nova_happy.png` not `IMG_1234.png`
- The system auto-generates: `nova_happy_a1b2c3d4.png`
- Multiple variants per tag are fine: `nova_happy_v1.png`, `nova_happy_v2.gif`

### Tag Selection Strategy
1. **Start with emotional tones** - Most impactful for conversation flow
2. **Add contextual topics** - For specialized personas (scientist, warrior, etc.)
3. **Temporal tags last** - Optional for time-aware personas

### Recommended Minimum Set
For any persona, upload at least:
- `neutral` (default/fallback)
- `happy` (positive interactions)
- `curious` (questions and learning)
- `serious` (important topics)

### Testing Your Setup
1. Upload avatars for different emotions
2. Test with sample messages in chat
3. Check browser console for `[Avatar]` debug logs
4. Verify avatar switches with 5-second debounce

---

## Troubleshooting

### Avatar not switching?
- Check browser console for errors
- Verify files exist in `data/avatars/{persona_id}/`
- Confirm file extensions are supported
- Check that at least one avatar is tagged or set as default

### Wrong emotion detected?
- Add more specific keywords to `EMOTION_KEYWORDS`
- Test with `/analyze/emotion?message=your text`
- Check for overlapping keywords between tags

### Storage full?
- Delete unused avatars via Admin → Persona
- Check current usage displayed in upload section
- Max is 500MB per persona

---

## API Reference

### Upload Endpoint
```
POST /admin/persona/media
Content-Type: multipart/form-data

Fields:
- persona_id: string (required)
- media: File (required, max 50MB)
- emotion_tag: string (one of 19 tags, default: "neutral")
- is_default: boolean (default: false)
- redirect_to: string (default: "/admin/persona")
```

### Delete Endpoint
```
POST /admin/persona/media/delete

Fields:
- persona_id: string (required)
- filename: string (required)
- redirect_to: string (optional)
```

### Analysis Endpoints
```
GET /analyze/emotion?message={text}
GET /avatar/emotional?message={text}&persona_id={id}
GET /test/emotion
```

---

## Summary

The 19-tag Dynamic Avatar System provides rich emotional expression for your Archiveum Personas:

- **11 emotional tones** for conversation dynamics
- **4 contextual topics** for subject-matter expertise
- **4 temporal contexts** for time-aware interactions
- **Reactive switching** based on user message analysis
- **Avatar persistence** — retains current avatar when no new emotion detected (no revert to default)
- **Debouncing** to prevent flicker
- **500MB storage** per persona for extensive avatar libraries
- **LLM avatar awareness** via descriptive metadata — enables natural visual references
- **Click-to-view lightbox** for full-size avatar viewing with video controls

Start with the core emotional tags, expand as needed, and create truly responsive, visually-aware AI companions!
