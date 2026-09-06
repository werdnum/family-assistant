import Foundation

/// The durable state of the one in-flight send turn, extracted out of
/// `runSendTurn`'s task-local variables so it OUTLIVES its transport task.
///
/// A send used to keep its cursor (`lastSeq`) and turn identity in locals and a
/// throwaway `ActiveChatTurn` value; when the transport task died (background,
/// dropped socket, superseded resume) that state died with it, so nothing could
/// reattach to a turn still running server-side. This object is retained by the
/// view model (`activeTurnSession`) and survives its transport task's death: it
/// is cleared only on turn completion or reconciliation, never on a transport
/// failure the send loop recovers from. That durability is the precondition for
/// M2's deliberate background teardown + foreground reattachment.
///
/// Scope is deliberately MINIMAL: only the currently-active turn's state lives
/// here. The multi-turn superseding-send bookkeeping (`registeredTurnIDs`,
/// `pendingSteersByTurnID`, etc.) stays in the view model, keyed by turn id, so
/// a superseding re-send's control state is untouched by this extraction.
@MainActor
final class ActiveTurnSession {
    /// The turn this session drives and the conversation it belongs to. Also the
    /// lightweight identity the steer/stop helpers key their per-turn control
    /// dictionaries on (see `identity`).
    let turnID: String
    let conversationID: String

    /// The optimistic assistant placeholder the send renders its tokens into,
    /// reconciled to the persisted row on completion.
    let assistantMessageID: String

    /// The original send inputs, retained so a resync/retry can reissue the SAME
    /// turn (never double-send) without reconstructing it from UI state.
    let prompt: String
    let attachments: [ChatAttachment]
    let profileID: String
    /// The intelligence level this turn was sent at, or nil for the profile's
    /// default. Retained for the same reason as `profileID`: a retry reissues the
    /// turn the user actually sent, not one rebuilt from whatever the picker now
    /// shows.
    let modelTier: String?
    /// The conversation summary before this send's optimistic bump, so a turn
    /// that fails to start can roll the list row back to it.
    let previousSummary: ChatConversationSummary?

    /// Highest stream seq applied for this turn: the send's resume cursor
    /// (`from_seq = lastAppliedSeq + 1`) and ack cursor. Mirrors the former
    /// `lastSeq` local so the cursor survives a transport drop.
    var lastAppliedSeq: Int?

    /// Identifies this send. A superseded (cancelled) transport task resuming
    /// across an await must not clobber the turn that replaced it; its tail work
    /// is gated on the view model's `currentStreamToken` still matching this.
    let streamToken: UUID

    init(
        turnID: String,
        conversationID: String,
        assistantMessageID: String,
        prompt: String,
        attachments: [ChatAttachment],
        profileID: String,
        modelTier: String?,
        previousSummary: ChatConversationSummary?,
        streamToken: UUID,
        lastAppliedSeq: Int? = nil
    ) {
        self.turnID = turnID
        self.conversationID = conversationID
        self.assistantMessageID = assistantMessageID
        self.prompt = prompt
        self.attachments = attachments
        self.profileID = profileID
        self.modelTier = modelTier
        self.previousSummary = previousSummary
        self.streamToken = streamToken
        self.lastAppliedSeq = lastAppliedSeq
    }
}
