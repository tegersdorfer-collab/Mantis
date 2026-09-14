import Foundation
import Security

final class MantisClient {
    static let shared = MantisClient()

    var baseURL: String {
        UserDefaults.standard.string(forKey: "mantis_base_url") ?? "http://macbook-air-von-timo.tail7e29ff.ts.net:7779"
    }


    // Store the credential in this app's Keychain, never in preferences or URLs.
    var apiToken: String {
        get {
            let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: "mantis-dashboard", kSecAttrAccount as String: baseURL,
                kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
            var result: CFTypeRef?
            guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
                  let data = result as? Data else { return "" }
            return String(data: data, encoding: .utf8) ?? ""
        }
        set {
            let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: "mantis-dashboard", kSecAttrAccount as String: baseURL]
            SecItemDelete(query as CFDictionary)
            if !newValue.isEmpty {
                var item = query
                item[kSecValueData as String] = Data(newValue.utf8)
                item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
                SecItemAdd(item as CFDictionary, nil)
            }
        }
    }

    private func authorizedRequest(_ url: URL) -> URLRequest {
        var request = URLRequest(url: url)
        request.setValue("Bearer \(apiToken)", forHTTPHeaderField: "Authorization")
        return request
    }

    private let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 15
        config.timeoutIntervalForResource = 90
        config.waitsForConnectivity = true
        return URLSession(configuration: config)
    }()

    func get<T: Decodable>(_ path: String) async throws -> T {
        let data = try await withRetry { try await self.session.data(for: self.authorizedRequest(try self.url(path))) }
        return try decode(T.self, data)
    }

    func post<B: Encodable, T: Decodable>(_ path: String, body: B) async throws -> T {
        var req = authorizedRequest(try url(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        let data = try await withRetry { try await self.session.data(for: req) }
        return try decode(T.self, data)
    }

    func put<B: Encodable, T: Decodable>(_ path: String, body: B) async throws -> T {
        var req = authorizedRequest(try url(path))
        req.httpMethod = "PUT"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        let data = try await withRetry { try await self.session.data(for: req) }
        return try decode(T.self, data)
    }

    func delete(_ path: String) async throws {
        var req = authorizedRequest(try url(path))
        req.httpMethod = "DELETE"
        _ = try await withRetry { try await self.session.data(for: req) }
    }

    var isReachable: Bool {
        get async {
            guard let u = URL(string: baseURL + "/health") else { return false }
            do { let (_, r) = try await session.data(for: authorizedRequest(u)); return (r as? HTTPURLResponse)?.statusCode == 200 }
            catch { return false }
        }
    }

    // Retry once after 1.5s for transient Tailscale connectivity blips
    @discardableResult
    private func withRetry(_ attempt: () async throws -> (Data, URLResponse)) async throws -> Data {
        do {
            let (data, response) = try await attempt()
            try check(response, data)
            return data
        } catch let err as MantisError {
            throw err
        } catch {
            try await Task.sleep(nanoseconds: 1_500_000_000)
            let (data, response) = try await attempt()
            try check(response, data)
            return data
        }
    }

    private func url(_ path: String) throws -> URL {
        guard let u = URL(string: baseURL + path) else { throw MantisError.invalidURL }
        return u
    }

    private func check(_ resp: URLResponse, _ data: Data) throws {
        guard let h = resp as? HTTPURLResponse, !(200...299).contains(h.statusCode) else { return }
        throw MantisError.http(h.statusCode, String(data: data, encoding: .utf8) ?? "")
    }

    private func decode<T: Decodable>(_ type: T.Type, _ data: Data) throws -> T {
        let d = JSONDecoder(); d.keyDecodingStrategy = .convertFromSnakeCase
        do { return try d.decode(T.self, from: data) }
        catch { throw MantisError.decodingError(error.localizedDescription) }
    }
}

enum MantisError: LocalizedError {
    case invalidURL, http(Int, String), decodingError(String), offline
    var errorDescription: String? {
        switch self {
        case .invalidURL: return "Ungültige URL"
        case .http(let c, let m): return "HTTP \(c): \(m)"
        case .decodingError(let m): return "Parse: \(m)"
        case .offline: return "Mantis nicht erreichbar"
        }
    }
}
