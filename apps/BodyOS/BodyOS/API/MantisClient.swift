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

    /// Credentials are sent only to the explicitly configured server.
    private func perform(_ make: (String) throws -> URLRequest) async throws -> Data {
        return try await withRetry { try await self.session.data(for: try make(self.baseURL)) }
    }

    func get<T: Decodable>(_ path: String) async throws -> T {
        let data = try await perform { base in
            var r = self.authorizedRequest(try self.makeURL(path, base: base)); r.httpMethod = "GET"; return r
        }
        return try decode(T.self, from: data)
    }

    func post<Body: Encodable, T: Decodable>(_ path: String, body: Body) async throws -> T {
        let payload = try JSONEncoder().encode(body)
        let data = try await perform { base in
            var r = self.authorizedRequest(try self.makeURL(path, base: base)); r.httpMethod = "POST"
            r.setValue("application/json", forHTTPHeaderField: "Content-Type"); r.httpBody = payload; return r
        }
        return try decode(T.self, from: data)
    }

    func put<Body: Encodable, T: Decodable>(_ path: String, body: Body) async throws -> T {
        let payload = try JSONEncoder().encode(body)
        let data = try await perform { base in
            var r = self.authorizedRequest(try self.makeURL(path, base: base)); r.httpMethod = "PUT"
            r.setValue("application/json", forHTTPHeaderField: "Content-Type"); r.httpBody = payload; return r
        }
        return try decode(T.self, from: data)
    }

    func postMultipart(_ path: String, imageData: Data, text: String?) async throws -> Data {
        var req = authorizedRequest(try makeURL(path))
        req.httpMethod = "POST"
        let boundary = UUID().uuidString
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        var body = Data()
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"image\"; filename=\"food.jpg\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: image/jpeg\r\n\r\n".data(using: .utf8)!)
        body.append(imageData)
        body.append("\r\n".data(using: .utf8)!)
        if let text, !text.isEmpty {
            body.append("--\(boundary)\r\n".data(using: .utf8)!)
            body.append("Content-Disposition: form-data; name=\"text\"\r\n\r\n".data(using: .utf8)!)
            body.append(text.data(using: .utf8)!)
            body.append("\r\n".data(using: .utf8)!)
        }
        body.append("--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = body
        return try await withRetry { try await self.session.data(for: req) }
    }

    func delete(_ path: String) async throws {
        _ = try await perform { base in
            var r = self.authorizedRequest(try self.makeURL(path, base: base)); r.httpMethod = "DELETE"; return r
        }
    }

    var isReachable: Bool {
        get async {
            for base in [baseURL] {
                if let url = URL(string: base + "/health"),
                   let (_, r) = try? await session.data(for: authorizedRequest(url)),
                   (r as? HTTPURLResponse)?.statusCode == 200 { return true }
            }
            return false
        }
    }

    // Retry once after 1.5s for transient Tailscale connectivity blips
    @discardableResult
    private func withRetry(_ attempt: () async throws -> (Data, URLResponse)) async throws -> Data {
        do {
            let (data, response) = try await attempt()
            try checkStatus(response, data: data)
            return data
        } catch let err as MantisError {
            throw err
        } catch {
            try await Task.sleep(nanoseconds: 1_500_000_000)
            let (data, response) = try await attempt()
            try checkStatus(response, data: data)
            return data
        }
    }

    private func makeURL(_ path: String, base: String? = nil) throws -> URL {
        guard let url = URL(string: (base ?? baseURL) + path) else { throw MantisError.invalidURL }
        return url
    }

    private func checkStatus(_ response: URLResponse, data: Data?) throws {
        guard let http = response as? HTTPURLResponse,
              !(200...299).contains(http.statusCode) else { return }
        let msg = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
        throw MantisError.httpError(http.statusCode, msg)
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        let dec = JSONDecoder()
        dec.keyDecodingStrategy = .convertFromSnakeCase
        do { return try dec.decode(T.self, from: data) }
        catch { throw MantisError.decodingError(error.localizedDescription) }
    }
}

enum MantisError: LocalizedError {
    case invalidURL, httpError(Int, String), decodingError(String), offline
    var errorDescription: String? {
        switch self {
        case .invalidURL: return "Ungültige URL"
        case .httpError(let c, let m): return "HTTP \(c): \(m)"
        case .decodingError(let m): return "Parse: \(m)"
        case .offline: return "Mantis nicht erreichbar"
        }
    }
}
