import SwiftUI

struct FlowSettingsView: View {
    @AppStorage("mantis_base_url") private var baseURL = "http://macbook-air-von-timo.tail7e29ff.ts.net:7779"
    @State private var apiToken = MantisClient.shared.apiToken
    @State private var isConnected: Bool? = nil
    @State private var isTesting = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Mantis Verbindung") {
                    SecureField("Dashboard-Token", text: $apiToken)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .onChange(of: apiToken) { _, value in MantisClient.shared.apiToken = value }
                        .onChange(of: baseURL) { _, _ in apiToken = MantisClient.shared.apiToken }

                    TextField("URL", text: $baseURL)
                        .keyboardType(.URL)
                        .autocapitalization(.none)
                        .autocorrectionDisabled()
                    Button {
                        Task { await testConnection() }
                    } label: {
                        HStack {
                            Text("Verbindung testen")
                            Spacer()
                            if isTesting {
                                ProgressView()
                            } else if let connected = isConnected {
                                Image(systemName: connected ? "checkmark.circle.fill" : "xmark.circle.fill")
                                    .foregroundStyle(connected ? .green : .red)
                            }
                        }
                    }
                }

                Section("Offline-Daten") {
                    Button("Cache leeren", role: .destructive) { clearCache() }
                }

                Section("Info") {
                    HStack { Text("Version"); Spacer(); Text("1.0").foregroundStyle(.secondary) }
                    HStack { Text("Aufgaben-Filter Standard"); Spacer(); Text("Aktiv").foregroundStyle(.secondary) }
                }
            }
            .navigationTitle("Einstellungen")
        }
    }

    private func testConnection() async {
        isTesting = true
        isConnected = await MantisClient.shared.isReachable
        isTesting = false
    }

    private func clearCache() {
        for key in ["cache_tasks", "cache_events", "cache_habits"] {
            UserDefaults.standard.removeObject(forKey: key)
        }
    }
}
