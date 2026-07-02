import Foundation
import Security

enum KeychainHelper {
    @discardableResult
    static func save(key: String, data: Data) -> Bool {
        delete(key: key)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        if status == errSecSuccess {
            return true
        }
        return saveFallback(key: key, data: data)
    }

    @discardableResult
    static func save(key: String, string: String) -> Bool {
        guard let data = string.data(using: .utf8) else { return false }
        return save(key: key, data: data)
    }

    static func read(key: String) -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecSuccess {
            return result as? Data
        }
        return readFallback(key: key)
    }

    static func readString(key: String) -> String? {
        guard let data = read(key: key) else { return nil }
        return String(data: data, encoding: .utf8)
    }

    @discardableResult
    static func delete(key: String) -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
        ]
        deleteFallback(key: key)
        return SecItemDelete(query as CFDictionary) == errSecSuccess
    }

    private static func saveFallback(key: String, data: Data) -> Bool {
        guard isTestFallbackEnabled else {
            return false
        }
        UserDefaults.standard.set(data, forKey: fallbackKey(key))
        return true
    }

    private static func readFallback(key: String) -> Data? {
        guard isTestFallbackEnabled else {
            return nil
        }
        return UserDefaults.standard.data(forKey: fallbackKey(key))
    }

    private static func deleteFallback(key: String) {
        guard isTestFallbackEnabled else {
            return
        }
        UserDefaults.standard.removeObject(forKey: fallbackKey(key))
    }

    private static func fallbackKey(_ key: String) -> String {
        "test_keychain_\(key)"
    }

    private static var isTestFallbackEnabled: Bool {
        #if DEBUG
        ProcessInfo.processInfo.arguments.contains("--ui-testing")
            || ProcessInfo.processInfo.arguments.contains("--live-ui-testing")
            || ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
        #else
        false
        #endif
    }
}
