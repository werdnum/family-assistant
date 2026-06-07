import XCTest

@testable import FamilyAssistant

final class NotesModelTests: XCTestCase {
    func testNativeNoteDecodesServerPayloadWithDefaults() throws {
        let data = Data(
            """
            {
              "title": "Wi-Fi Password",
              "content": "The network password is in the drawer."
            }
            """.utf8
        )

        let note = try JSONDecoder().decode(NativeNote.self, from: data)

        XCTAssertEqual(note.title, "Wi-Fi Password")
        XCTAssertEqual(note.content, "The network password is in the drawer.")
        XCTAssertTrue(note.includeInPrompt)
        XCTAssertEqual(note.attachmentIds, [])
        XCTAssertEqual(note.visibilityLabels, [])
        XCTAssertFalse(note.isSkill)
        XCTAssertNil(note.skillName)
        XCTAssertNil(note.skillDescription)
    }

    func testNativeNoteDecodesFullServerPayload() throws {
        let data = Data(
            """
            {
              "title": "Shopping",
              "content": "Buy milk",
              "include_in_prompt": false,
              "attachment_ids": ["receipt"],
              "visibility_labels": ["family"],
              "is_skill": true,
              "skill_name": "shopping",
              "skill_description": "Shopping notes"
            }
            """.utf8
        )

        let note = try JSONDecoder().decode(NativeNote.self, from: data)

        XCTAssertEqual(note.title, "Shopping")
        XCTAssertEqual(note.content, "Buy milk")
        XCTAssertFalse(note.includeInPrompt)
        XCTAssertEqual(note.attachmentIds, ["receipt"])
        XCTAssertEqual(note.visibilityLabels, ["family"])
        XCTAssertTrue(note.isSkill)
        XCTAssertEqual(note.skillName, "shopping")
        XCTAssertEqual(note.skillDescription, "Shopping notes")
    }

    func testNativeNoteSaveRequestEncodesServerKeys() throws {
        let request = NativeNoteSaveRequest(
            title: "Shopping",
            content: "Buy milk",
            includeInPrompt: false,
            originalTitle: "Old Shopping",
            attachmentIds: ["receipt"],
            visibilityLabels: ["family"]
        )

        let data = try JSONEncoder().encode(request)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])

        XCTAssertEqual(object["title"] as? String, "Shopping")
        XCTAssertEqual(object["content"] as? String, "Buy milk")
        XCTAssertEqual(object["include_in_prompt"] as? Bool, false)
        XCTAssertEqual(object["original_title"] as? String, "Old Shopping")
        XCTAssertEqual(object["attachment_ids"] as? [String], ["receipt"])
        XCTAssertEqual(object["visibility_labels"] as? [String], ["family"])
    }
}
