import SwiftUI

struct NoteEditorView: View {
    @Environment(AuthManager.self) private var authManager

    let originalTitle: String?
    let onSaved: (String) -> Void
    let onCancel: () -> Void

    @State private var title = ""
    @State private var content = ""
    @State private var includeInPrompt = true
    @State private var attachmentIds: [String] = []
    @State private var visibilityLabels: [String] = []
    @State private var isLoading = false
    @State private var isSaving = false
    @State private var errorMessage: String?

    private var isEditing: Bool {
        originalTitle != nil
    }

    private var canSave: Bool {
        !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !isLoading
            && !isSaving
    }

    var body: some View {
        Group {
            if isLoading {
                ProgressView("Loading note...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                Form {
                    if let errorMessage {
                        Section {
                            Text(errorMessage)
                                .foregroundStyle(.red)
                        }
                    }

                    Section("Title") {
                        TextField("Note title", text: $title)
                            .textInputAutocapitalization(.words)
                    }

                    Section("Content") {
                        TextEditor(text: $content)
                            .font(.body)
                            .frame(minHeight: 260)
                    }

                    Section {
                        Toggle("Include in system prompt", isOn: $includeInPrompt)
                    } footer: {
                        Text("When enabled, the assistant can include this note directly in conversation context.")
                    }

                    if !attachmentIds.isEmpty || !visibilityLabels.isEmpty {
                        Section {
                            if !attachmentIds.isEmpty {
                                Label("\(attachmentIds.count) attachments", systemImage: "paperclip")
                            }
                            if !visibilityLabels.isEmpty {
                                Label(visibilityLabels.joined(separator: ", "), systemImage: "tag")
                            }
                        } header: {
                            Text("Preserved Metadata")
                        } footer: {
                            Text("Attachment editing is still handled by the web UI, but these values are preserved when saving.")
                        }
                    }

                    Section {
                        Button {
                            Task { await saveNote() }
                        } label: {
                            if isSaving {
                                ProgressView()
                                    .frame(maxWidth: .infinity)
                            } else {
                                Text("Save Note")
                                    .frame(maxWidth: .infinity)
                            }
                        }
                        .disabled(!canSave)

                        Button("Cancel", role: .cancel, action: onCancel)
                    }
                }
            }
        }
        .navigationTitle(isEditing ? "Edit Note" : "Add Note")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: originalTitle) {
            await loadExistingNoteIfNeeded()
        }
    }

    private func loadExistingNoteIfNeeded() async {
        guard let originalTitle else {
            title = ""
            content = ""
            includeInPrompt = true
            attachmentIds = []
            visibilityLabels = []
            return
        }

        isLoading = true
        errorMessage = nil
        do {
            let note = try await NotesAPIClient(authManager: authManager).getNote(title: originalTitle)
            title = note.title
            content = note.content
            includeInPrompt = note.includeInPrompt
            attachmentIds = note.attachmentIds
            visibilityLabels = note.visibilityLabels
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func saveNote() async {
        let trimmedTitle = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedContent = content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedTitle.isEmpty, !trimmedContent.isEmpty else {
            errorMessage = "Title and content are required."
            return
        }

        isSaving = true
        errorMessage = nil
        do {
            let request = NativeNoteSaveRequest(
                title: trimmedTitle,
                content: trimmedContent,
                includeInPrompt: includeInPrompt,
                originalTitle: originalTitle,
                attachmentIds: attachmentIds,
                visibilityLabels: visibilityLabels
            )
            try await NotesAPIClient(authManager: authManager).saveNote(request)
            onSaved(trimmedTitle)
        } catch {
            errorMessage = error.localizedDescription
        }
        isSaving = false
    }
}
