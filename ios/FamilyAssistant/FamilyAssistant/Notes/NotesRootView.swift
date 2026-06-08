import SwiftUI

struct NotesRootView: View {
    let route: NotesRoute
    let onRouteChange: (NotesRoute) -> Void
    let onOpenChat: () -> Void
    let onLogout: () -> Void

    @State private var listReloadToken = UUID()

    var body: some View {
        NavigationStack {
            content
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        leadingToolbarButton
                    }

                    ToolbarItemGroup(placement: .topBarTrailing) {
                        routeActionButton
                        AppSettingsMenu(onLogout: onLogout)
                    }
                }
        }
    }

    @ViewBuilder
    private var content: some View {
        switch route {
        case .list:
            NotesListView(
                reloadToken: listReloadToken,
                onAdd: { onRouteChange(.add) },
                onSelect: { onRouteChange(.detail(title: $0)) },
                onEdit: { onRouteChange(.edit(title: $0)) }
            )
        case .detail(let title):
            NoteDetailView(
                title: title,
                onEdit: { onRouteChange(.edit(title: title)) },
                onDeleted: {
                    refreshList()
                    onRouteChange(.list)
                }
            )
        case .add:
            NoteEditorView(
                originalTitle: nil,
                onSaved: { savedTitle in
                    refreshList()
                    onRouteChange(.detail(title: savedTitle))
                },
                onCancel: { onRouteChange(.list) }
            )
        case .edit(let title):
            NoteEditorView(
                originalTitle: title,
                onSaved: { savedTitle in
                    refreshList()
                    onRouteChange(.detail(title: savedTitle))
                },
                onCancel: { onRouteChange(.detail(title: title)) }
            )
        }
    }

    @ViewBuilder
    private var leadingToolbarButton: some View {
        switch route {
        case .list:
            Button {
                onOpenChat()
            } label: {
                Label("Chat", systemImage: "message")
            }
        case .detail, .add, .edit:
            Button {
                onRouteChange(.list)
            } label: {
                Label("Notes", systemImage: "chevron.left")
            }
        }
    }

    @ViewBuilder
    private var routeActionButton: some View {
        switch route {
        case .list:
            Button {
                onRouteChange(.add)
            } label: {
                Label("Add Note", systemImage: "plus")
            }
        case .detail(let title):
            Button {
                onRouteChange(.edit(title: title))
            } label: {
                Label("Edit", systemImage: "pencil")
            }
        case .add, .edit:
            EmptyView()
        }
    }

    private func refreshList() {
        listReloadToken = UUID()
    }
}
