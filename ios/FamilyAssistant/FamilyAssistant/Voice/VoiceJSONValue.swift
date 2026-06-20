import Foundation

/// Additional read accessors used by the voice protocol codec to walk decoded
/// ``JSONValue`` trees (Gemini server frames, tool-call arguments). ``JSONValue``
/// and its `stringValue`/`objectValue`/`doubleValue` accessors are defined with
/// the chat models; these complete the set the codec needs.
extension JSONValue {
    /// Member access for `.object` values; `nil` for any other case or a missing key.
    subscript(key: String) -> JSONValue? {
        if case .object(let dictionary) = self {
            return dictionary[key]
        }
        return nil
    }

    var boolValue: Bool? {
        if case .bool(let value) = self { return value }
        return nil
    }

    var arrayValue: [JSONValue]? {
        if case .array(let value) = self { return value }
        return nil
    }
}
