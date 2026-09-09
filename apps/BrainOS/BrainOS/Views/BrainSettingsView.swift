import SwiftUI

struct BrainSettingsView: View {
    @AppStorage("mantis_base_url") private var baseURL: String = "http://macbook-air-von-timo.tail7e29ff.ts.net:7779"
    @State private var apiToken = MantisClient.shared.apiToken
    @State private var testResult: String?
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

                    TextField("Base URL", text: $baseURL)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)

                    Button {
                        Task { await testConnection() }
                    } label: {
                        HStack {
                            if isTesting { ProgressView().scaleEffect(0.8) }
                            else { Image(systemName: "network") }
                            Text("Verbindung testen")
                        }
                    }

                    if let result = testResult {
                        Text(result)
                            .font(.footnote)
                            .foregroundColor(result.contains("✓") ? .green : .red)
                    }
                }

                Section("Info") {
                    LabeledContent("App", value: "BrainOS")
                    LabeledContent("Version", value: "1.0")
                    LabeledContent("Backend", value: "Mantis FastAPI :7779")
                }
            }
            .navigationTitle("Einstellungen")
        }
    }

    private func testConnection() async {
        isTesting = true; testResult = nil
        let ok = await MantisClient.shared.isReachable
        testResult = ok ? "✓ Verbunden mit Mantis" : "✗ Nicht erreichbar – URL prüfen"
        isTesting = false
    }
}
