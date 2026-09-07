# 3D Models

**What's here:** generating 3D models from a description or a photo, refining and texturing them,
and getting them ready to print.

The assistant can create 3D models through [Meshy](https://www.meshy.ai/). Ask in plain language —
"make me a 3D model of a hexagonal plant pot", or send a photo and say "turn this into a 3D model".

## What you can ask for

- **From a description** — "generate a 3D model of a low-poly fox".
- **From a photo** — send one image, or several of the same object from different angles, and ask
  for a model of it.
- **Refinements** — ask for a higher-detail version of a draft, a new texture, or a different file
  format (GLB, FBX, OBJ, USDZ, STL, 3MF).
- **Characters** — rigging adds a skeleton to a humanoid model, and animations can be applied to a
  rigged one.
- **Printing** — the assistant can check whether a model will print reliably on an FDM printer,
  repair the topology if it won't, and prepare a multi-colour 3MF for an AMS/MMU printer.

Generation is not instant. The assistant starts a job and checks back on it; for a detailed model
that can take a few minutes. You can ask "how's that model going?" at any point, or ask it to cancel
one.

## Costs and confirmation

Meshy charges credits per job, so the assistant asks you to confirm before it starts one. Checking
the status of a job, listing your models, and checking your remaining balance are free and don't
need confirmation. Ask "how many Meshy credits do I have left?" whenever you want to know.

## If it isn't available

3D generation needs a Meshy API key, which whoever runs your Family Assistant has to set up. If the
assistant says it doesn't have the tools for this, that's why — ask your operator.
