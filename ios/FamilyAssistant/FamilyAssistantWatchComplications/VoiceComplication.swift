import SwiftUI
import WidgetKit

private struct VoiceEntry: TimelineEntry {
    let date: Date
}

private struct VoiceProvider: TimelineProvider {
    func placeholder(in _: Context) -> VoiceEntry {
        VoiceEntry(date: .now)
    }

    func getSnapshot(in _: Context, completion: @escaping (VoiceEntry) -> Void) {
        completion(VoiceEntry(date: .now))
    }

    func getTimeline(in _: Context, completion: @escaping (Timeline<VoiceEntry>) -> Void) {
        completion(Timeline(entries: [VoiceEntry(date: .now)], policy: .never))
    }
}

private struct VoiceComplicationView: View {
    @Environment(\.widgetFamily) private var family

    var body: some View {
        Group {
            switch family {
            case .accessoryInline:
                Label("Ask Assistant", systemImage: "mic.fill")
            case .accessoryRectangular:
                Label {
                    VStack(alignment: .leading) {
                        Text("Family Assistant").font(.headline)
                        Text("Start voice")
                    }
                } icon: { Image(systemName: "mic.fill") }
            default:
                Image(systemName: "mic.fill")
                    .font(.title2)
                    .widgetAccentable()
            }
        }
        .containerBackground(for: .widget) { Color.clear }
        .widgetURL(WatchVoiceLaunch.url)
        .accessibilityLabel("Start Family Assistant voice mode")
    }
}

@main
struct VoiceComplication: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "FamilyAssistantVoice", provider: VoiceProvider()) { _ in
            VoiceComplicationView()
        }
        .configurationDisplayName("Family Assistant Voice")
        .description("Start a voice conversation with your assistant.")
        .supportedFamilies([.accessoryCircular, .accessoryCorner, .accessoryInline, .accessoryRectangular])
    }
}
