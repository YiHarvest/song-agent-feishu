from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..kernel.intent import IntentSpec
from ..kernel.models import AttachmentRef, ExecutionContext, IntentResult
from ..kernel.module import ContributionSink, ModuleManifest
from ..ports.attachments import AsrPort, AttachmentStorePort, DocumentParserPort, VisionPort
from ..runtime.providers import ProviderRegistry


class AttachmentUnderstandArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attachment: AttachmentRef
    instruction: str = Field(default="", max_length=10_000)
    language: str = Field(default="auto", max_length=32)


class AttachmentUnderstandHandler:
    def __init__(self, providers: ProviderRegistry, kind: str) -> None:
        self.providers = providers
        self.kind = kind

    async def __call__(self, context: ExecutionContext, arguments: BaseModel) -> IntentResult:
        args = AttachmentUnderstandArguments.model_validate(arguments)
        store: AttachmentStorePort = self.providers.get(AttachmentStorePort)
        path = await store.resolve_path(
            args.attachment,
            principal=context.envelope.principal,
            conversation=context.envelope.conversation,
        )
        if self.kind == "image_analyze":
            provider: VisionPort = self.providers.get(VisionPort)
            data = await provider.analyze(path, args.attachment.media_type, args.instruction)
        elif self.kind == "audio_transcribe":
            asr: AsrPort = self.providers.get(AsrPort)
            data = await asr.transcribe(
                path,
                filename=args.attachment.filename,
                media_type=args.attachment.media_type,
                language=args.language,
            )
        else:
            parser: DocumentParserPort = self.providers.get(DocumentParserPort)
            data = await parser.parse(
                path,
                filename=args.attachment.filename,
                media_type=args.attachment.media_type,
                instruction=args.instruction,
            )
        return IntentResult.success(code=f"attachment.{self.kind}.completed", data=data)


class AttachmentModule:
    manifest = ModuleManifest(
        module_id="attachment",
        version="1.0.0",
        owns_namespaces=frozenset({"attachment"}),
    )

    def __init__(self, providers: ProviderRegistry) -> None:
        self.providers = providers

    def contribute(self, sink: ContributionSink) -> None:
        for operation, description in (
            ("image_analyze", "Analyze an image attachment."),
            ("audio_transcribe", "Transcribe an audio attachment."),
            ("document_parse", "Parse a document attachment."),
        ):
            sink.add_intent(
                IntentSpec(
                    intent_id=f"attachment.{operation}",
                    version=1,
                    module_id="attachment",
                    description=description,
                    arguments_model=AttachmentUnderstandArguments,
                    result_model=IntentResult,
                    handler=AttachmentUnderstandHandler(self.providers, operation),
                    agent_exposed=True,
                    execution_timeout_seconds=180,
                )
            )
