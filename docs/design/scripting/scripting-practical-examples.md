# Scripting Language Practical Examples

This document provides concrete examples of how scripting would work in Family Assistant across different use cases.

## Event Listener Examples

### Example 1: Smart Temperature Alerts

**Use Case**: Only alert about high temperatures during waking hours and not more than once per hour.

**Current Approach**(requires LLM):

```yaml
listeners:

  - name: "Temperature Monitor"
    conditions:

      - source: home_assistant
        entity_id: sensor.outside_temperature
    action:
      type: wake_llm
      config:
        prompt: |
          The temperature is {{event.state}}°C.
          Only send an alert if:

          1. Temperature is above 30°C
          2. It's between 6 AM and 10 PM
          3. We haven't sent an alert in the last hour

```

**With CEL**(no LLM needed):

```yaml
listeners:

  - name: "Temperature Monitor"
    conditions:

      - source: home_assistant
        entity_id: sensor.outside_temperature
        cel_filter: |
          event.state > 30 &&
          time.hour >= 6 && time.hour <= 22 &&
          time.since(state.last_temp_alert) > duration('1h')
    action:
      type: notification
      config:
        message: "High temperature alert: {{event.state}}°C"
        update_state:
          last_temp_alert: "{{time.now}}"

```

### Example 2: Complex Motion Automation

**Use Case**: Motion-activated lights with different behaviors based on time and occupancy.

**Starlark Script**:

```python
def handle_motion(event, ctx):
    """Smart motion-based lighting control"""

    room = event.metadata.room
    motion_state = event.state

    # Get current conditions
    is_night = ctx.time.hour < 6 or ctx.time.hour > 20
    is_home = ctx.state.get("occupancy.home", False)
    room_lights = ctx.tools.get_lights(room)

    if motion_state == "detected":
        if is_night:
            # Dim lights at night
            brightness = 20 if is_home else 50
            ctx.tools.turn_on_lights(room, brightness=brightness, color_temp=2700)
        elif not room_lights.any_on():
            # Normal brightness during day
            ctx.tools.turn_on_lights(room, brightness=100)

        # Schedule auto-off
        timeout = 5 if is_night else 15
        ctx.schedule.add_task(
            f"turn_off_{room}_lights",
            delay_minutes=timeout,
            action=lambda: ctx.tools.turn_off_lights(room)
        )

    elif motion_state == "clear" and is_night:
        # Immediately turn off in night mode
        ctx.tools.turn_off_lights(room)

```

## Security Planning Example

**Use Case**: Process untrusted email content safely by planning actions first.

**Starlark Security Script**:

```python
def plan_email_processing(email, ctx):
    """Plan how to process an email before exposing content to LLM"""

    # Analyze email metadata safely
    sender = email.sender.lower()
    subject = email.subject

    # Define processing rules
    rules = []

    # Check if sender is trusted
    trusted_domains = ctx.db.get_user_data("trusted_email_domains")
    is_trusted = any(sender.endswith(domain) for domain in trusted_domains)

    if not is_trusted:
        rules.append({
            "action": "quarantine",
            "reason": "Untrusted sender"
        })

    # Check for suspicious patterns
    suspicious_patterns = [
        "verify your account",
        "suspended",
        "click here immediately"
    ]

    if any(pattern in subject.lower() for pattern in suspicious_patterns):
        rules.append({
            "action": "flag_suspicious",
            "confidence": 0.8
        })

    # Check attachments
    if email.has_attachments:
        for attachment in email.attachments:
            if attachment.extension in [".exe", ".scr", ".vbs"]:
                rules.append({
                    "action": "block_attachment",
                    "filename": attachment.name
                })

    # Return processing plan
    return {
        "is_safe": len([r for r in rules if r["action"] in ["quarantine", "block_attachment"]]) == 0,
        "rules": rules,
        "processing_notes": f"Email from {sender} analyzed with {len(rules)} rules applied"
    }

```

## Multi-Step Task Automation

**Use Case**: Weekly meal planning with shopping list generation.

**Starlark Workflow Script**:

```python
def weekly_meal_planning(ctx):
    """Automate weekly meal planning and shopping list generation"""

    # Get family preferences
    preferences = ctx.db.get_user_data("meal_preferences")
    dietary_restrictions = ctx.db.get_user_data("dietary_restrictions")

    # Check calendar for the week
    week_events = ctx.tools.get_calendar_events(days=7)
    busy_days = [e.date for e in week_events if e.duration > 4]

    # Get recent meals to avoid repetition
    recent_meals = ctx.db.get_notes(query="meal_plan", limit=14)
    recent_recipes = [m.metadata.get("recipe") for m in recent_meals]

    # Plan meals based on schedule
    meal_plan = {}
    for day in range(7):
        date = ctx.time.today + duration(f"{day}d")

        if date in busy_days:
            # Quick meal for busy days
            meal_plan[date] = {
                "type": "quick",
                "time": 30,
                "complexity": "low"
            }
        else:
            # Regular meal
            meal_plan[date] = {
                "type": "regular",
                "time": 60,
                "complexity": "medium"
            }

    # Generate meal suggestions (this could call a specialized LLM)
    suggestions = ctx.tools.generate_meal_suggestions(
        meal_requirements=meal_plan,
        preferences=preferences,
        restrictions=dietary_restrictions,
        exclude=recent_recipes
    )

    # Create shopping list
    shopping_items = set()
    for meal in suggestions.values():
        shopping_items.update(meal["ingredients"])

    # Check pantry inventory
    pantry = ctx.db.get_notes(query="pantry inventory")
    available_items = {item.title for item in pantry}

    # Final shopping list
    to_buy = shopping_items - available_items

    # Save the plan
    ctx.db.create_note(
        title=f"Meal Plan - Week of {ctx.time.today}",
        content=format_meal_plan(suggestions),
        metadata={"type": "meal_plan", "week": ctx.time.week}
    )

    ctx.db.create_note(
        title=f"Shopping List - {ctx.time.today}",
        content=format_shopping_list(to_buy),
        metadata={"type": "shopping_list"}
    )

    # Notify user
    ctx.notify.send(
        f"Weekly meal plan created with {len(to_buy)} items to buy. "
        f"{len(busy_days)} quick meals planned for busy days."
    )

```

## Tool Enhancement Example

**Use Case**: Enhance the note search tool with custom ranking logic.

**CEL Expression for Custom Ranking**:

```text
// Custom note ranking based on recency, relevance, and user behavior
(
  // Base relevance score from search
  note.search_score *1.0 +

  // Boost recent notes
  (time.since(note.updated) < duration('7d') ? 0.3 : 0.0) +
  (time.since(note.updated) < duration('24h') ? 0.2 : 0.0) +

  // Boost frequently accessed notes
  (note.access_count > 10 ? 0.2 : 0.0) +

  // Boost notes with specific tags
  (note.tags.contains('important') ? 0.5 : 0.0) +
  (note.tags.contains('project-' + context.current_project) ? 0.3 : 0.0) +

  // Penalize archived notes
  (note.tags.contains('archived') ? -0.5 : 0.0)
)

```

## Event Aggregation Example

**Use Case**: Aggregate multiple sensor readings before taking action.

**Starlark Event Aggregator**:

```python
def aggregate_environment_sensors(event, ctx):
    """Aggregate multiple sensor readings for intelligent climate control"""

    # Store current reading
    sensor_data = ctx.state.get("sensor_buffer", {})
    sensor_data[event.entity_id] = {
        "value": event.state,
        "timestamp": ctx.time.now
    }

    # Clean old readings (> 5 minutes)
    sensor_data = {
        k: v for k, v in sensor_data.items()
        if ctx.time.since(v["timestamp"]) < duration("5m")
    }

    ctx.state.set("sensor_buffer", sensor_data)

    # Check if we have enough recent data
    required_sensors = [
        "sensor.living_room_temperature",
        "sensor.living_room_humidity",
        "sensor.outside_temperature",
        "sensor.presence_detected"
    ]

    if all(s in sensor_data for s in required_sensors):
        # Calculate comfort index
        indoor_temp = sensor_data["sensor.living_room_temperature"]["value"]
        humidity = sensor_data["sensor.living_room_humidity"]["value"]
        outdoor_temp = sensor_data["sensor.outside_temperature"]["value"]
        occupied = sensor_data["sensor.presence_detected"]["value"]

        comfort_index = calculate_comfort(indoor_temp, humidity)

        # Make decisions based on aggregated data
        if occupied and comfort_index < 0.6:
            if outdoor_temp < indoor_temp - 5:
                # Too hot inside, cool outside
                ctx.tools.climate_control("ventilate")
            elif indoor_temp < 20:
                ctx.tools.climate_control("heat")
            elif indoor_temp > 26:
                ctx.tools.climate_control("cool")

        # Log the decision
        ctx.db.create_note(
            title="Climate Control Decision",
            content=f"Comfort: {comfort_index:.2f}, Action: {action}",
            metadata={
                "sensor_data": sensor_data,
                "comfort_index": comfort_index
            }
        )

def calculate_comfort(temp, humidity):
    """Calculate comfort index (0-1) based on temperature and humidity"""
    # Ideal: 22°C and 50% humidity
    temp_deviation = abs(temp - 22) / 10
    humidity_deviation = abs(humidity - 50) / 50
    return max(0, 1 - (temp_deviation + humidity_deviation) / 2)

```

## Integration with LLM Tools

**Use Case**: Script that coordinates multiple LLM tool calls efficiently.

**Starlark Coordinator Script**:

```python
def research_and_summarize(topic, ctx):
    """Coordinate research across multiple sources"""

    results = {}

    # Search different sources in parallel
    tasks = [
        ("notes", lambda: ctx.tools.search_notes(topic)),
        ("calendar", lambda: ctx.tools.search_calendar(topic)),
        ("email", lambda: ctx.tools.search_email(topic)),
        ("web", lambda: ctx.tools.web_search(topic))
    ]

    # Execute searches
    for source, search_func in tasks:
        try:
            results[source] = search_func()
        except Exception as e:
            results[source] = {"error": str(e)}

    # Filter and rank results
    relevant_results = []
    for source, items in results.items():
        if "error" not in items:
            for item in items[:5]:  # Top 5 from each source
                relevant_results.append({
                    "source": source,
                    "relevance": calculate_relevance(item, topic),
                    "content": item
                })

    # Sort by relevance
    relevant_results.sort(key=lambda x: x["relevance"], reverse=True)

    # Only send top results to LLM for summarization
    top_results = relevant_results[:10]

    if top_results:
        summary = ctx.tools.llm_summarize(
            context=top_results,
            prompt=f"Summarize findings about {topic}"
        )

        # Save research results
        ctx.db.create_note(
            title=f"Research: {topic}",
            content=summary,
            metadata={
                "sources": [r["source"] for r in top_results],
                "result_count": len(relevant_results)
            }
        )

        return summary
    else:
        return f"No relevant information found about {topic}"

def calculate_relevance(item, topic):
    """Calculate relevance score for search result"""
    # Simple keyword matching (could be enhanced)
    keywords = topic.lower().split()
    content = str(item).lower()
    matches = sum(1 for kw in keywords if kw in content)
    return matches / len(keywords)

```

## Conclusion

These examples demonstrate how scripting can:

1. **Reduce LLM calls**- Simple logic handled by CEL/Starlark
2. **Improve reliability**- Deterministic execution for critical paths
3. **Enable complex workflows**- Multi-step processes with conditional logic
4. **Enhance security**- Pre-plan actions before processing untrusted data
5. **Provide customization**- User-defined logic without code changes

The combination of CEL for simple expressions and Starlark for complex logic provides a powerful and secure scripting environment for Family Assistant.
