# AI Engine contracts

The AI Engine is a provider-neutral boundary. It currently contains typed
interfaces and static placeholder adapters only. No LLM, prompt execution,
network call, or content-generation behavior is implemented.

Capabilities:

- `PromptBuilderInterface`
- `ExplanationEngineInterface`
- `AdaptiveReadingInterface`
- `NovelModeInterface`
- `ImageDecisionInterface`
- `SummaryGeneratorInterface`
- `FlashcardGeneratorInterface`
- `QuizGeneratorInterface`

Each interface accepts `AiInput` and returns `AiPlaceholderResponse`. The
corresponding `*Placeholder` classes return a stable `pending` response that
identifies the capability. Provider adapters can later implement the protocols
without changing module consumers.