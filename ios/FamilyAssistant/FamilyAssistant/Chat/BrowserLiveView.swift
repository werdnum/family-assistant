import Observation
import SwiftUI
import UIKit

@MainActor
@Observable
final class BrowserLiveViewController {
    private(set) var latestFrame: ChatBrowserViewFrame?
    private(set) var currentImage: UIImage?
    private(set) var isLoadingImage = false

    private let fetchImageData: @MainActor (String) async throws -> Data
    private var loadTask: Task<Void, Never>?
    private var loadGeneration = 0

    init(fetchImageData: @escaping @MainActor (String) async throws -> Data) {
        self.fetchImageData = fetchImageData
    }

    func apply(frame: ChatBrowserViewFrame?) {
        guard let frame else {
            reset()
            return
        }
        if latestFrame?.frameID == frame.frameID, currentImage != nil {
            latestFrame = frame
            return
        }
        latestFrame = frame
        loadTask?.cancel()
        loadGeneration += 1
        let generation = loadGeneration
        let path = frame.imageURL
        isLoadingImage = true

        loadTask = Task { [weak self] in
            guard let self else { return }
            defer {
                if self.loadGeneration == generation {
                    self.isLoadingImage = false
                }
            }
            do {
                let data = try await self.fetchImageData(path)
                guard !Task.isCancelled, self.loadGeneration == generation else {
                    return
                }
                if let image = UIImage(data: data) {
                    self.currentImage = image
                }
            } catch {
                // Best-effort live preview: keep displaying the previous frame.
            }
        }
    }

    func reset() {
        loadTask?.cancel()
        loadTask = nil
        loadGeneration += 1
        latestFrame = nil
        currentImage = nil
        isLoadingImage = false
    }
}

struct BrowserLiveViewCard: View {
    var controller: BrowserLiveViewController
    let isRunning: Bool
    let isBrowserProfile: Bool
    var compact = false

    @State private var isCollapsed = false
    @State private var showingFullScreen = false

    var body: some View {
        if let frame = controller.latestFrame {
            VStack(spacing: 0) {
                headerBar(for: frame)

                if !isCollapsed {
                    viewportBody(for: frame)
                }
            }
            .background(Color(.secondarySystemGroupedBackground))
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(Color.primary.opacity(0.08), lineWidth: 1)
            )
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .accessibilityIdentifier("live-browser-view")
            .sheet(isPresented: $showingFullScreen) {
                BrowserLiveFullScreenSheet(
                    controller: controller,
                    isRunning: isRunning
                )
            }
        } else if isBrowserProfile && isRunning {
            HStack(spacing: 8) {
                ProgressView()
                    .controlSize(.small)
                Text("Live browser")
                    .font(.caption.weight(.semibold))
                Text("Starting session…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(Color(.secondarySystemBackground))
            .overlay(alignment: .bottom) { Divider() }
            .accessibilityIdentifier("live-browser-waiting")
        }
    }

    private func headerBar(for frame: ChatBrowserViewFrame) -> some View {
        HStack(spacing: 6) {
            if isRunning {
                Circle()
                    .fill(Color.green)
                    .frame(width: 7, height: 7)
                Text("Live")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.green)
            } else {
                Image(systemName: "macwindow")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text("Browser")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
            }

            if let backendKind = frame.backendKind, !backendKind.isEmpty {
                Text(backendKind.capitalized)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(Color.primary.opacity(0.06))
                    .clipShape(Capsule())
            }

            HStack(spacing: 4) {
                Image(systemName: "globe")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                Text(frame.displayURL)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(.systemBackground).opacity(0.85))
            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))

            Button {
                showingFullScreen = true
            } label: {
                Image(systemName: "arrow.up.left.and.arrow.down.right")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(width: 24, height: 24)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Expand live browser view")

            Button {
                withAnimation(.easeInOut(duration: 0.18)) {
                    isCollapsed.toggle()
                }
            } label: {
                Image(systemName: isCollapsed ? "chevron.down" : "chevron.up")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(width: 24, height: 24)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(isCollapsed ? "Show live browser view" : "Hide live browser view")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(Color(.tertiarySystemGroupedBackground).opacity(0.8))
    }

    @ViewBuilder
    private func viewportBody(for frame: ChatBrowserViewFrame) -> some View {
        Button {
            showingFullScreen = true
        } label: {
            ZStack {
                Color.black.opacity(0.04)
                if let image = controller.currentImage {
                    Image(uiImage: image)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .frame(maxWidth: .infinity, maxHeight: compact ? 185 : 220)
                } else {
                    HStack(spacing: 8) {
                        ProgressView()
                            .controlSize(.small)
                        Text("Loading preview…")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .frame(height: 110)
                }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Open full-size live browser screenshot of \(frame.displayURL)")
    }
}

private struct BrowserLiveFullScreenSheet: View {
    var controller: BrowserLiveViewController
    let isRunning: Bool

    @Environment(\.dismiss) private var dismiss
    @State private var scale: CGFloat = 1.0
    @State private var settledScale: CGFloat = 1.0

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if let frame = controller.latestFrame {
                    HStack(spacing: 8) {
                        if isRunning {
                            Circle()
                                .fill(Color.green)
                                .frame(width: 8, height: 8)
                            Text("Live")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.green)
                        }
                        if let toolName = frame.formattedToolName {
                            Text(toolName)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Text(frame.displayURL)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(Color(.secondarySystemBackground))
                    Divider()
                }

                GeometryReader { geometry in
                    ScrollView([.horizontal, .vertical], showsIndicators: false) {
                        Group {
                            if let image = controller.currentImage {
                                Image(uiImage: image)
                                    .resizable()
                                    .aspectRatio(contentMode: .fit)
                                    .frame(
                                        width: geometry.size.width * scale,
                                        height: geometry.size.height * scale
                                    )
                                    .onTapGesture(count: 2) {
                                        withAnimation(.easeInOut(duration: 0.2)) {
                                            if scale > 1.01 {
                                                scale = 1.0
                                                settledScale = 1.0
                                            } else {
                                                scale = 2.0
                                                settledScale = 2.0
                                            }
                                        }
                                    }
                            } else {
                                ProgressView("Loading screenshot…")
                                    .frame(width: geometry.size.width, height: geometry.size.height)
                            }
                        }
                        .frame(minWidth: geometry.size.width, minHeight: geometry.size.height)
                    }
                    .gesture(
                        MagnifyGesture()
                            .onChanged { value in
                                scale = min(max(settledScale * value.magnification, 1.0), 4.0)
                            }
                            .onEnded { _ in
                                settledScale = scale
                            }
                    )
                }
                .background(Color.black.opacity(0.95))
            }
            .navigationTitle("Live Browser")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}
