<!-- Mirrored from https://ai.google.dev/gemini-api/docs/models.md.txt on 2026-11-09.
     Fetch that URL directly if this looks out of date. -->

> ## AI Agent Context
> Get the complete documentation index at: https://aistudio.google.com/llms.txt
> Use this file to discover all available pages.
> For the full documentation in a single file, fetch: https://aistudio.google.com/llms-full.txt

# Models

## Gemini 4  {% id="gemini-4" %}

{% card-grid columns="2" %}
  {% card description="Our flagship reasoning model with a shared 1M context window and long decodes up to 1M tokens." title="Gemini 4 Argon" variant="model-card" %}
  {% section class="model-specs" %}
  *   *Model code* **gemini-4-argon**
  *   *Context* **1M tokens**
  *   *Max output* **65K / 1M tokens**
  *   *Modalities* **Text, Image, Audio, Video, PDF**

  {% /section %}

  {% section class="card-actions" %}
  {% button href="https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon" variant="primary" %}Try in AI Studio{% /button %} {% button href="/docs/models/gemini-4-argon" icon="arrow_forward" tooltip="View models" variant="icon-primary" /%}

  {% /section %}

  {% /card %}

{% /card-grid %}

## All Gemini 4 models  {% id="all-gemini-4-models" %}

{% table variant="model" %}
  * Model
  * Endpoint

  ---

  * [Gemini 4 Argon](/docs/models/gemini-4-argon)
  * `gemini-4-argon`
{% /table %}

## Gemini 3  {% id="gemini-3" %}

{% card-grid %}
  {% card badge="New" description="Our most intelligent Flash model, engineered for long-horizon software engineering, autonomous agents, and complex enterprise workflows." href="/docs/models/gemini-3.8-flash" icon="spark" title="Gemini 3.8 Flash" variant="model" /%}

  {% card badge="New" description="Default Live API model for most low-latency voice agent experiences without reasoning delays." href="/docs/models/gemini-3.8-live" icon="settings_voice" title="Gemini 3.8 Live" variant="model" /%}

  {% card badge="New" description="High-reasoning Live API model for voice interactions, recommended when higher background reasoning is required." href="/docs/models/gemini-3.8-live-extended-thinking" icon="psychology" title="Gemini 3.8 Live Extended Thinking" variant="model" /%}

  {% card badge="New" description="Flagship creative text-to-speech model for studio-grade voice fidelity, expressive acting, Voice design, and Voice replication." href="/docs/models/gemini-3.8-flash-tts" icon="graphic_eq" title="Gemini 3.8 Flash TTS" variant="model" /%}

  {% card badge="New" description="Fast, cost-efficient text-to-speech model for high-volume production, real-time voice agent cascades, and Voice replication." href="/docs/models/gemini-3.8-flash-lite-tts" icon="graphic_eq" title="Gemini 3.8 Flash-Lite TTS" variant="model" /%}

  {% card badge="Stable" description="Our previous-generation Flash model, balancing speed and multimodal capabilities across general agentic and everyday tasks." href="/docs/models/gemini-3.6-flash" icon="spark" title="Gemini 3.6 Flash" variant="model" /%}

  {% card badge="Stable" description="Our fastest, most cost-effective 3.5 model for high-throughput execution." href="/docs/models/gemini-3.5-flash-lite" icon="bolt" title="Gemini 3.5 Flash-Lite" variant="model" /%}

  {% card badge="Stable" description="Frontier-class performance rivaling larger models at a fraction of the cost." href="/docs/models/gemini-3.1-flash-lite" icon="bolt" title="Gemini 3.1 Flash-Lite" variant="model" /%}

  {% card badge="New" description="High-efficiency image generation and editing model with improved visual quality, typography, and multi-turn consistency." href="/docs/models/gemini-nano-banana-2.1" icon="banana_spark" title="Nano Banana 2.1" variant="model" /%}

  {% card badge="Stable" description="Powerful, high-efficiency image generation and editing, optimized for speed and high-volume use cases." href="/docs/models/gemini-3.1-flash-image" icon="banana_spark" title="Nano Banana 2" variant="model" /%}

  {% card badge="Stable" description="Ultra-low latency and cost-effective image generation and editing, designed for high-volume interactive use cases." href="/docs/models/gemini-3.1-flash-lite-image" icon="banana_spark" title="Nano Banana 2 Lite" variant="model" /%}

  {% card badge="Stable" description="State-of-the-art image generation and editing models for highly contextual native image creation." href="/docs/models/gemini-3-pro-image" icon="banana_spark" title="Nano Banana Pro" variant="model" /%}

  {% card badge="Stable" description="Low-latency speech-to-text model with utterance-based language detection, speaker diarization, and word timestamps." href="/docs/models/gemini-3.5-transcribe" icon="speech_to_text" title="Gemini 3.5 Transcribe" variant="model" /%}

  {% card badge="New" description="Generate and edit up to 15 seconds of video with native audio, keyframe interpolation, video extension, and character consistency." href="/docs/models/gemini-omni-flash" icon="movie_filter" title="Gemini Omni Flash" variant="model" /%}

  {% card badge="Preview" description="Frontier-class performance rivaling larger models at a fraction of the cost." href="/docs/models/gemini-3-flash-preview" icon="spark" title="Gemini 3 Flash" variant="model" /%}

  {% card badge="Preview" description="Low-latency, real-time speech to speech translation model that supports 70+ languages." href="/docs/models/gemini-3.5-live-translate-preview" icon="translate" title="Gemini 3.5 Live Translate" variant="model" /%}

  {% card badge="Preview" description="Legacy Live API preview model. We recommend updating to Gemini 3.8 Live." href="/docs/models/gemini-3.1-flash-live-preview" icon="settings_voice" title="Gemini 3.1 Flash Live" variant="model" /%}

  {% card badge="Preview" description="Legacy TTS preview model. We recommend updating to Gemini 3.8 Flash TTS or Gemini 3.8 Flash-Lite TTS." href="/docs/models/gemini-3.1-flash-tts-preview" icon="graphic_eq" title="Gemini 3.1 Flash TTS" variant="model" /%}

{% /card-grid %}

## All Gemini 3 models  {% id="all-gemini-3-models" %}

{% table variant="model" %}
  * Model
  * Endpoint

  ---

  * [Gemini 3.8 Flash](/docs/models/gemini-3.8-flash)
  * `gemini-3.8-flash`

  ---

  * [Gemini 3.8 Live](/docs/models/gemini-3.8-live)
  * `gemini-3.8-live`

  ---

  * [Gemini 3.8 Live Extended Thinking](/docs/models/gemini-3.8-live-extended-thinking)
  * `gemini-3.8-live-extended-thinking`

  ---

  * [Gemini 3.8 Flash TTS](/docs/models/gemini-3.8-flash-tts)
  * `gemini-3.8-flash-tts`

  ---

  * [Gemini 3.8 Flash-Lite TTS](/docs/models/gemini-3.8-flash-lite-tts)
  * `gemini-3.8-flash-lite-tts`

  ---

  * [Gemini 3.6 Flash](/docs/models/gemini-3.6-flash)
  * `gemini-3.6-flash`

  ---

  * [Gemini 3.5 Flash-Lite](/docs/models/gemini-3.5-flash-lite)
  * `gemini-3.5-flash-lite`

  ---

  * [Gemini 3.1 Flash-Lite](/docs/models/gemini-3.1-flash-lite)
  * `gemini-3.1-flash-lite`

  ---

  * [Nano Banana 2.1](/docs/models/gemini-nano-banana-2.1)
  * `gemini-nano-banana-2.1`

  ---

  * [Nano Banana 2](/docs/models/gemini-3.1-flash-image)
  * `gemini-3.1-flash-image`

  ---

  * [Nano Banana 2 Lite](/docs/models/gemini-3.1-flash-lite-image)
  * `gemini-3.1-flash-lite-image`

  ---

  * [Nano Banana Pro](/docs/models/gemini-3-pro-image)
  * `gemini-3-pro-image`

  ---

  * [Gemini 3.5 Transcribe](/docs/models/gemini-3.5-transcribe)
  * `gemini-3.5-transcribe`   
    `gemini-3.5-transcribe-live`

  ---

  * [Gemini Omni Flash](/docs/models/gemini-omni-flash)
  * `gemini-omni-1.2-flash`   
    `gemini-omni-1.1-flash`

  ---

  * [Gemini 3.1 Pro](/docs/models/gemini-3.1-pro-preview)
  * `gemini-3.1-pro-preview`

  ---

  * [Gemini 3 Flash](/docs/models/gemini-3-flash-preview)
  * `gemini-3-flash-preview`

  ---

  * [Gemini 3.5 Live Translate](/docs/models/gemini-3.5-live-translate-preview)
  * `gemini-3.5-live-translate-preview`

  ---

  * [Gemini 3.1 Flash Live](/docs/models/gemini-3.1-flash-live-preview)
  * `gemini-3.1-flash-live-preview`

  ---

  * [Gemini 3.1 Flash TTS](/docs/models/gemini-3.1-flash-tts-preview)
  * `gemini-3.1-flash-tts-preview`
{% /table %}

## Gemini 2.5 Flash  {% id="gemini-25-flash" %}

{% callout type="caution" %}
  `gemini-2.5-flash` is [deprecated](/docs/deprecations#gemini-2.5-flash-models)
  and is no longer available to select in Google AI Studio (API access to the 2.5
  models is limited to existing users). Effective December 1, 2026, all API
  requests to `gemini-2.5-flash` will automatically route to
  [`gemini-3.5-flash-lite`](/docs/models/gemini-3.5-flash-lite). Update your model
  string to `gemini-3.5-flash-lite` and review the
  [Gemini 3.5 Flash-Lite migration guide](/docs/whats-new-gemini-3.6#migrate-to-gemini-3-5-flash-lite)
  before December 1, 2026.

{% /callout %}

{% table variant="model" %}
  * Model
  * Description
  * Endpoint

  ---

  * [Gemini 2.5 Flash](/docs/models/gemini-2.5-flash)
  * Our best price-performance model for low-latency, high-volume tasks that
    require reasoning.
  * `gemini-2.5-flash`

  ---

  * [Nano Banana](/docs/models/gemini-2.5-flash-image)
  * State-of-the-art native image generation and editing designed for fast,
    creative workflows.
  * `gemini-2.5-flash-image`

  ---

  * [Gemini 2.5 Flash Live](/docs/models/gemini-2.5-flash-native-audio-preview-12-2025)
  * Optimized for real-time conversational agents with sub-second native audio
    streaming.
  * `gemini-2.5-flash-native-audio-preview-12-2025`

  ---

  * [Gemini 2.5 Flash TTS](/docs/models/gemini-2.5-flash-preview-tts)
  * Controllable text-to-speech audio generation with fine control over style
    and pacing.
  * `gemini-2.5-flash-preview-tts`
{% /table %}

## Gemini 2.5 Flash-Lite  {% id="gemini-25-flash-lite" %}

{% callout type="note" %}
  To ensure reliable performance for everyone, we are limiting access to the 2.5
  models to users who have actively used them in the past. `gemini-2.5-flash-lite`
  is not deprecated and will continue to be served until further notice through
  the API. For any new projects, use our latest models: 3.5 Flash-Lite or 3.8
  Flash. This helps us maintain sufficient capacity for both ongoing legacy
  workflows and new applications.

{% /callout %}

{% table variant="model" %}
  * Model
  * Description
  * Endpoint

  ---

  * [Gemini 2.5 Flash-Lite](/docs/models/gemini-2.5-flash-lite)
  * The fastest and most budget-friendly multimodal model in the 2.5 family.
  * `gemini-2.5-flash-lite`
{% /table %}

## Gemini 2.5 Pro  {% id="gemini-25-pro" %}

{% callout type="caution" %}
  `gemini-2.5-pro` is [deprecated](/docs/deprecations#gemini-2.5-pro-models) and
  is no longer available to select in Google AI Studio (API access to the 2.5
  models is limited to existing users). Effective December 1, 2026, all API
  requests to `gemini-2.5-pro` will automatically route to
  [`gemini-3.8-flash`](/docs/models/gemini-3.8-flash). Update your model string to
  `gemini-3.8-flash` and review the
  [Gemini 3.8 Flash migration guide](/docs/whats-new-gemini-3.8#migrate-to-gemini-3-8-flash)
  before December 1, 2026.

{% /callout %}

{% table variant="model" %}
  * Model
  * Description
  * Endpoint

  ---

  * [Gemini 2.5 Pro](/docs/models/gemini-2.5-pro)
  * Our most advanced model for complex tasks, featuring deep reasoning and
    coding capabilities in the 2.5 family.
  * `gemini-2.5-pro`

  ---

  * [Gemini 2.5 Pro TTS](/docs/models/gemini-2.5-pro-preview-tts)
  * High-fidelity speech synthesis optimized for quality in structured workflows
    like podcasts and audiobooks.
  * `gemini-2.5-pro-preview-tts`
{% /table %}

## Audio models  {% id="audio-models" %}

*This section contains all audio models, including ones that may already be
listed in other sections*

{% table variant="model" %}
  * Model
  * Description
  * Endpoint

  ---

  * [Gemini 3.8 Live](/docs/models/gemini-3.8-live)
  * The default option for most low-latency voice agent experiences and real-time
    dialogue without reasoning delays.
  * `gemini-3.8-live`

  ---

  * [Gemini 3.8 Live Extended Thinking](/docs/models/gemini-3.8-live-extended-thinking)
  * Our high-reasoning audio-to-audio model, recommended when higher background
    reasoning is required during live interactions.
  * `gemini-3.8-live-extended-thinking`

  ---

  * [Gemini 3.8 Flash TTS](/docs/models/gemini-3.8-flash-tts)
  * Flagship creative text-to-speech model for studio-grade voice fidelity,
    expressive acting, and regional dialects across 130 languages.
  * `gemini-3.8-flash-tts`

  ---

  * [Gemini 3.8 Flash-Lite TTS](/docs/models/gemini-3.8-flash-lite-tts)
  * Fast, cost-efficient text-to-speech model built for high-throughput
    production workloads across 101 languages.
  * `gemini-3.8-flash-lite-tts`

  ---

  * [Gemini 3.1 Flash Live](/docs/models/gemini-3.1-flash-live-preview)
  * Legacy audio-to-audio preview model. We recommend updating to Gemini 3.8 Live.
  * `gemini-3.1-flash-live-preview`

  ---

  * [Gemini 3.1 Flash TTS](/docs/models/gemini-3.1-flash-tts-preview)
  * Legacy text-to-speech preview model. We recommend updating to Gemini 3.8
    Flash TTS or Gemini 3.8 Flash-Lite TTS.
  * `gemini-3.1-flash-tts-preview`

  ---

  * [Gemini 2.5 Flash Live](/docs/models/gemini-2.5-flash-native-audio-preview-12-2025)
  * Our flagship Live API model for low-latency, bidirectional voice and video
    agents with native audio reasoning.
  * `gemini-2.5-flash-native-audio-preview-12-2025`

  ---

  * [Gemini 2.5 Flash TTS](/docs/models/gemini-2.5-flash-preview-tts)
  * Fast and controllable text-to-speech for low-latency, cost-efficient
    applications and real-time assistants.
  * `gemini-2.5-flash-preview-tts`

  ---

  * [Gemini 2.5 Pro TTS](/docs/models/gemini-2.5-pro-preview-tts)
  * High-fidelity speech synthesis optimized for quality in structured workflows
    like podcasts and audiobooks.
  * `gemini-2.5-pro-preview-tts`
{% /table %}

## Generative media models  {% id="generative-media-models" %}

*This section contains all generative media models, including ones that may
already be listed in other sections*

{% table variant="model" %}
  * Model
  * Description
  * Endpoint

  ---

  * [Nano Banana 2.1](/docs/models/gemini-nano-banana-2.1)
  * High-efficiency image generation and editing model with improved visual
    quality, typography, and multi-turn consistency.
  * `gemini-nano-banana-2.1`

  ---

  * [Nano Banana 2](/docs/models/gemini-3.1-flash-image)
  * High-efficiency production-scale visual creation, combining the intelligence
    of the Gemini 3 series with lightning-fast generation speeds.
  * `gemini-3.1-flash-image`

  ---

  * [Nano Banana 2 Lite](/docs/models/gemini-3.1-flash-lite-image)
  * Our fastest and cheapest Gemini image model, engineered for velocity and
    scale where speed and cost are the primary operational constraints. Not
    optimized for multiple reference inputs or multi-turn sequential editing.
  * `gemini-3.1-flash-lite-image`

  ---

  * [Nano Banana Pro](/docs/models/gemini-3-pro-image)
  * A professional design engine with a reasoning core for studio-quality 4K
    visuals, complex layouts, and precise text rendering.
  * `gemini-3-pro-image`

  ---

  * [Gemini Omni Flash](/docs/models/gemini-omni-flash)
  * Fast video generation and editing up to 15 seconds with native audio,
    keyframe interpolation, and video extension.
  * `gemini-omni-1.2-flash`   
    `gemini-omni-1.1-flash`

  ---

  * [Nano Banana](/docs/models/gemini-2.5-flash-image)
  * State-of-the-art native image generation and editing designed for fast,
    creative workflows.
  * `gemini-2.5-flash-image`

  ---

  * Imagen 4 (Shut down)
  * Text-to-image model featuring fast and ultra-fast generation (shut down).
  * `imagen-4.0-generate`
{% /table %}

## Music generation models  {% id="music-models" %}

*This section contains all music generation models, including ones that may
already be listed in other sections*

{% table variant="model" %}
  * Model
  * Description
  * Endpoint

  ---

  * [Lyria 3.5](/docs/models/lyria-3.5)
  * Our flagship music generation model, optimized for full-length songs with
    complex structural coherence.
  * `lyria-3.5`

  ---

  * [Lyria 3 Clip](/docs/models/lyria-3-clip-preview)
  * Optimized for generating short musical clips, loops, and previews up to 30
    seconds.
  * `lyria-3-clip-preview`

  ---

  * [Lyria 3 Pro](/docs/models/lyria-3-pro-preview)
  * Previous generation music generation model for full-length songs.
  * `lyria-3-pro-preview`

  ---

  * [Lyria RealTime](/docs/models/lyria-realtime-exp)
  * High-fidelity music generation model providing granular creative control and
    real-time streaming capabilities.
  * `lyria-realtime-exp`
{% /table %}

## Tool and agent models  {% id="tool-and-agent-models" %}

{% table variant="model" %}
  * Model
  * Description
  * Endpoint

  ---

  * [Gemini Deep Research](/docs/models/deep-research-preview-04-2026)
  * An agentic model that autonomously plans and executes multi-step research
    across hundreds of sources to produce cited, interactive reports.
  * `deep-research-preview-04-2026`

  ---

  * [Gemini Deep Research Max](/docs/models/deep-research-max-preview-04-2026)
  * Maximum comprehensiveness for automated context gathering and synthesis
    across hundreds of sources.
  * `deep-research-max-preview-04-2026`

  ---

  * [Antigravity Agent](/docs/models/antigravity-preview-09-2026)
  * A general-purpose managed agent that autonomously plans, reasons, runs code,
    manages files, and browses the web inside a secure, isolated Linux sandbox.
  * `antigravity-preview-09-2026`
{% /table %}

## Specialized task models  {% id="specialized-task-models" %}

{% table variant="model" %}
  * Model
  * Description
  * Endpoint

  ---

  * [Gemini Embedding 2](/docs/models/gemini-embedding-2)
  * Our first multimodal embedding model, mapping text, images, video, audio,
    and PDFs into a unified embedding space for advanced semantic search and RAG
    systems.
  * `gemini-embedding-2`

  ---

  * [Gemini Embedding](/docs/models/gemini-embedding-001)
  * High-dimensional vector representations for advanced semantic search, text
    classification, and RAG systems.
  * `gemini-embedding-001`

  ---

  * [Gemini Robotics ER 2](/docs/robotics-overview)
  * Embodied reasoning model delivering advanced video understanding, spatial
    reasoning, multi-step tool orchestration, and multi-robot collaboration for
    robotics tasks.
  * `gemini-robotics-er-2-preview`
{% /table %}

## Previous models  {% id="previous-models" %}

{% callout type="warning" %}
  These models are [deprecated](/docs/deprecations) and will be shut down soon;
  migrate to newer models to prevent service interruptions.

{% /callout %}

{% table variant="model" %}
  * Model
  * Description
  * Endpoint

  ---

  * [Gemini Robotics ER 1.6](/docs/models/gemini-robotics-er-1.6-preview) (Shut down)
  * Advanced embodied reasoning model that understands physical spaces and plans
    multi-step tasks for robotic agents with new capabilities like instrument
    reading, improved spatial and physical reasoning.
  * `gemini-robotics-er-1.6-preview`

  ---

  * [Computer Use](/docs/models/gemini-2.5-computer-use-preview-10-2025) (Shut down)
  * A specialized model that can "see" a digital screen and perform UI actions
    like clicking, typing, and navigating to automate complex browser tasks.
  * `gemini-2.5-computer-use-preview-10-2025`

  ---

  * [Gemini 2.0 Flash](/docs/models/gemini-2.0-flash) (Shut down)
  * Our second generation workhorse model, with next-gen features and improved
    capabilities, including superior speed, native tool use, and a 1M token
    context window.
  * `gemini-2.0-flash`

  ---

  * [Gemini 2.0 Flash-Lite](/docs/models/gemini-2.0-flash-lite) (Shut down)
  * Our fastest second generation model, optimized for cost efficiency and low
    latency.
  * `gemini-2.0-flash-lite`

  ---

  * [Gemini 3.1 Flash-Lite Preview](/docs/models/gemini-3.1-flash-lite-preview)
    (Shut down)
  * Our most cost-efficient multimodal model, offering the fastest performance
    for high-frequency, lightweight tasks.
  * `gemini-3.1-flash-lite-preview`

  ---

  * [Gemini 3 Pro Preview](/docs/models/gemini-3-pro-preview) (Shut down)
  * Our state-of-the-art reasoning model, with advanced multimodal
    understanding.
  * `gemini-3-pro-preview`
{% /table %}

## Model version name patterns  {% id="model-versions" %}

Gemini models are available in either *stable*, *preview*, *latest*, or
*experimental* versions.

{% callout type="note" %}
  Note: The following list refers to the model string naming convention as of
  September, 2025. Models released prior to that may have different naming
  conventions. Refer to the exact model string if you are using an older model.

{% /callout %}

### Stable  {% class="hide-from-toc" id="stable" %}

Points to a specific stable model. Stable models usually don't change. Most
production apps should use a specific stable model.

For example: `gemini-3.6-flash`.

### Preview  {% class="hide-from-toc" id="preview" %}

Points to a preview model which may be used for production. Preview models will
typically have billing enabled, might come with more restrictive rate limits and
will be deprecated with at least 2 weeks notice.

For example: `gemini-2.5-flash-preview-09-2025`.

### Latest  {% class="hide-from-toc" id="latest" %}

Points to the latest release for a specific model variation. This can be a
stable, preview or experimental release. This alias will get hot-swapped with
every new release of a specific model variation. A **2-week notice** will be
provided through email before the version behind latest is changed.

For example: `gemini-flash-latest`.

### Experimental  {% class="hide-from-toc" id="experimental" %}

Points to an experimental model which will typically not be suitable for
production use and come with more restrictive rate limits. We release
experimental models to gather feedback and get our latest updates into the hands
of developers quickly.

Experimental models are not stable and availability of model endpoints is
subject to change.

## Model deprecations  {% id="model-deprecations" %}

For information about model deprecations, visit the [Gemini
deprecations](/docs/deprecations) page.

