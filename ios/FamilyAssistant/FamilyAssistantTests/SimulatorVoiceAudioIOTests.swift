import XCTest

@testable import FamilyAssistant

final class SimulatorVoiceAudioIOTests: XCTestCase {
    #if targetEnvironment(simulator)
    func testScriptedPromptsFallBackWhenScriptIsEmpty() {
        XCTAssertEqual(SimulatorVoiceAudioIO.prompts(fromScript: nil, fallback: "Default"), ["Default"])
        XCTAssertEqual(SimulatorVoiceAudioIO.prompts(fromScript: "", fallback: "Default"), ["Default"])
        XCTAssertEqual(SimulatorVoiceAudioIO.prompts(fromScript: "  ||\t|| ", fallback: "Default"), ["Default"])
    }

    func testScriptedPromptsDropEmptySegments() {
        XCTAssertEqual(
            SimulatorVoiceAudioIO.prompts(fromScript: " First prompt || || Second prompt ", fallback: "Default"),
            ["First prompt", "Second prompt"]
        )
    }
    #endif
}
