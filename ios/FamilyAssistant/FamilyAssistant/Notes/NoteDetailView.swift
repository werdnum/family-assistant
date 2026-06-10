import SwiftUI

struct NoteDetailView: View {
    @Environment(AuthManager.self) private var authManager

    let title: String
    let onEdit: () -> Void
    let onDeleted: () -> Void

    @State private var note: NativeNote?
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var isConfirmingDelete = false

    var body: some View {
        Group {
            if isLoading && note == nil {
                ProgressView("Loading note...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let errorMessage, note == nil {
                ContentUnavailableView {
                    Label("Could Not Load Note", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(errorMessage)
                } actions: {
                    Button("Retry") {
                        Task { await loadNote() }
                    }
                }
            } else if let note {
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(note.title)
                                .font(.largeTitle.bold())
                                .textSelection(.enabled)

                            HStack(spacing: 10) {
                                NotePromptStatusBadge(includeInPrompt: note.includeInPrompt)

                                if note.isSkill {
                                    Label("Skill", systemImage: "sparkle")
                                        .font(.caption)
                                        .foregroundStyle(.purple)
                                }

                                if !note.attachmentIds.isEmpty {
                                    Label("\(note.attachmentIds.count) attachments", systemImage: "paperclip")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }

                        Text(note.content)
                            .font(.body)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)

                        if !note.visibilityLabels.isEmpty {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("Visibility")
                                    .font(.headline)
                                FlowLayout(items: note.visibilityLabels) { label in
                                    Text(label)
                                        .font(.caption)
                                        .padding(.horizontal, 8)
                                        .padding(.vertical, 4)
                                        .background(.thinMaterial, in: Capsule())
                                }
                            }
                        }

                        Button(role: .destructive) {
                            isConfirmingDelete = true
                        } label: {
                            Label("Delete Note", systemImage: "trash")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                        .padding(.top, 12)
                    }
                    .padding()
                }
            } else {
                ProgressView("Loading note...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .navigationTitle("Note")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: title) {
            await loadNote()
        }
        .confirmationDialog(
            "Delete \(note?.title ?? "note")?",
            isPresented: $isConfirmingDelete,
            titleVisibility: .visible
        ) {
            Button("Delete Note", role: .destructive) {
                Task { await deleteNote() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This removes the note from Family Assistant.")
        }
        .alert(
            "Could Not Delete Note",
            isPresented: Binding(
                get: { errorMessage != nil && note != nil },
                set: { if !$0 { errorMessage = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(errorMessage ?? "")
        }
    }

    private func loadNote() async {
        isLoading = true
        errorMessage = nil
        note = nil
        do {
            note = try await NotesAPIClient(authManager: authManager).getNote(title: title)
        } catch {
            errorMessage = error.localizedDescription
            ErrorReporter.shared.report(error, component: "Notes.detail.load")
        }
        isLoading = false
    }

    private func deleteNote() async {
        guard let note else { return }
        do {
            try await NotesAPIClient(authManager: authManager).deleteNote(title: note.title)
            onDeleted()
        } catch {
            errorMessage = error.localizedDescription
            ErrorReporter.shared.report(error, component: "Notes.detail.delete")
        }
    }
}

private struct FlowLayout<Content: View>: View {
    let items: [String]
    let content: (String) -> Content

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack {
                ForEach(items, id: \.self, content: content)
            }
            VStack(alignment: .leading) {
                ForEach(items, id: \.self, content: content)
            }
        }
    }
}
