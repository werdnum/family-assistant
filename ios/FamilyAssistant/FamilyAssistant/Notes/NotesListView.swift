import SwiftUI

struct NotesListView: View {
    @Environment(AuthManager.self) private var authManager

    let reloadToken: UUID
    let onAdd: () -> Void
    let onSelect: (String) -> Void
    let onEdit: (String) -> Void

    @State private var notes: [NativeNote] = []
    @State private var searchText = ""
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var notePendingDeletion: NativeNote?

    private var filteredNotes: [NativeNote] {
        let trimmedSearch = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedSearch.isEmpty else { return notes }
        return notes.filter { note in
            note.title.localizedCaseInsensitiveContains(trimmedSearch)
                || note.content.localizedCaseInsensitiveContains(trimmedSearch)
        }
    }

    var body: some View {
        Group {
            if isLoading && notes.isEmpty {
                ProgressView("Loading notes...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let errorMessage, notes.isEmpty {
                ContentUnavailableView {
                    Label("Could Not Load Notes", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(errorMessage)
                } actions: {
                    Button("Retry") {
                        Task { await loadNotes() }
                    }
                }
            } else if filteredNotes.isEmpty {
                ContentUnavailableView {
                    Label(searchText.isEmpty ? "No Notes" : "No Matching Notes", systemImage: "note.text")
                } description: {
                    Text(searchText.isEmpty ? "Add a note to store family context." : "Try a different search.")
                } actions: {
                    if searchText.isEmpty {
                        Button("Add Note", action: onAdd)
                    }
                }
            } else {
                List {
                    ForEach(filteredNotes) { note in
                        NotesListRow(note: note)
                            .contentShape(Rectangle())
                            .onTapGesture {
                                onSelect(note.title)
                            }
                            .swipeActions(edge: .trailing) {
                                Button(role: .destructive) {
                                    notePendingDeletion = note
                                } label: {
                                    Label("Delete", systemImage: "trash")
                                }

                                Button {
                                    onEdit(note.title)
                                } label: {
                                    Label("Edit", systemImage: "pencil")
                                }
                                .tint(.blue)
                            }
                    }
                }
                .listStyle(.plain)
            }
        }
        .navigationTitle("Notes")
        .searchable(text: $searchText, prompt: "Search notes")
        .refreshable {
            await loadNotes()
        }
        .task(id: reloadToken) {
            await loadNotes()
        }
        .confirmationDialog(
            "Delete \(notePendingDeletion?.title ?? "note")?",
            isPresented: Binding(
                get: { notePendingDeletion != nil },
                set: { if !$0 { notePendingDeletion = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Delete Note", role: .destructive) {
                Task { await deletePendingNote() }
            }
            Button("Cancel", role: .cancel) {
                notePendingDeletion = nil
            }
        } message: {
            Text("This removes the note from Family Assistant.")
        }
    }

    private func loadNotes() async {
        isLoading = true
        errorMessage = nil
        do {
            notes = try await NotesAPIClient(authManager: authManager).listNotes()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    private func deletePendingNote() async {
        guard let note = notePendingDeletion else { return }
        notePendingDeletion = nil
        do {
            try await NotesAPIClient(authManager: authManager).deleteNote(title: note.title)
            notes.removeAll { $0.title == note.title }
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct NotesListRow: View {
    let note: NativeNote

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(note.title)
                    .font(.headline)
                    .lineLimit(2)

                Spacer()

                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }

            Text(previewText)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(3)

            HStack(spacing: 8) {
                NotePromptStatusBadge(includeInPrompt: note.includeInPrompt)

                if note.isSkill {
                    Label("Skill", systemImage: "sparkle")
                        .font(.caption)
                        .foregroundStyle(.purple)
                }

                if !note.attachmentIds.isEmpty {
                    Label("\(note.attachmentIds.count)", systemImage: "paperclip")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 6)
    }

    private var previewText: String {
        let collapsed = note.content
            .replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return collapsed.isEmpty ? "No content" : collapsed
    }
}

struct NotePromptStatusBadge: View {
    let includeInPrompt: Bool

    var body: some View {
        Label(
            includeInPrompt ? "In Prompt" : "Searchable",
            systemImage: includeInPrompt ? "checkmark.circle.fill" : "magnifyingglass.circle"
        )
        .font(.caption)
        .foregroundStyle(includeInPrompt ? .green : .secondary)
    }
}
