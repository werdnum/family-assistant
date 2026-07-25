# Smart Home (Home Assistant)

**What's here:** controlling devices, asking about the state of your home, and what the assistant
knows about who's where.

This requires a Home Assistant integration configured by your operator. For security camera review,
see [camera_integration.md](camera_integration.md); for reacting automatically to events, see
[automations.md](automations.md).

## Controlling devices

- "Turn on the kitchen lights."
- "Is the garage door closed?"
- "Set the thermostat to 70 degrees."
- "What's the temperature in the baby's room?"

Use the names your devices have in Home Assistant ("Living Room Lamp", "Downstairs Thermostat"). If
you're not sure, ask what's available, or check with whoever manages your Home Assistant setup.

Under the hood the assistant can run any Home Assistant action, so it isn't limited to switches and
thermostats — it can activate scenes ("activate movie night"), run scripts ("run my bedtime
script"), play media, or send Home Assistant notifications. Anything Home Assistant itself can do is
available.

## Who's home

If you have device trackers or person entities configured, the assistant knows who is home and who
is away, can tell you distances to known locations like work or school, and can use that context in
its answers ("is it worth starting dinner?").

## Camera snapshots

- "Show me the front door camera."
- "Is anyone at the front porch?"

Snapshots come back as images you can then ask about — "is that a delivery van?" — because the
assistant can pass an attachment straight into its vision tools.

## Historical data

- "Show me the pool temperature over the last 5 days."
- "Download the thermostat history for this week."

History comes back as data you can chart. See [data_visualization.md](data_visualization.md), and
note that sensor history often contains `unavailable` or `unknown` values that need filtering before
plotting.

## Watching for things to happen

- "Let me know when Alex arrives home."
- "Alert me if the garage door opens after 10pm."
- "Watch for when the washing machine finishes."

These create automations. See [automations.md](automations.md).

## Troubleshooting

- **The assistant can't find a device.** Use the exact entity name from Home Assistant. Ask "what
  lights are available?" to see what it can reach.
- **Nothing responds at all.** Ask your operator to check the Home Assistant integration —
  specifically the URL and access token.
- **Brief gaps in event monitoring.** The assistant reconnects to Home Assistant automatically after
  a dropped connection; you may notice a short interruption while it does.
