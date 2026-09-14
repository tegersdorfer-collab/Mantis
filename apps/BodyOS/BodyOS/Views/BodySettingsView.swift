import SwiftUI

struct BodySettingsView: View {
    @AppStorage("mantis_base_url") private var baseURL = "http://macbook-air-von-timo.tail7e29ff.ts.net:7779"
    @State private var apiToken = MantisClient.shared.apiToken
    @State private var isConnected: Bool? = nil
    @State private var isTesting = false
    @State private var profile: TrainingProfile?
    @StateObject private var hk = HealthKitManager.shared

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

                Section("HealthKit") {
                    HStack {
                        Text("Status")
                        Spacer()
                        Text(hk.isAuthorized ? "Verbunden" : "Nicht autorisiert")
                            .foregroundStyle(hk.isAuthorized ? .green : .secondary)
                    }
                    if !hk.isAuthorized {
                        Button("Zugriff erlauben") { Task { await hk.requestAuthorization() } }
                    }
                    if let last = hk.lastSync {
                        HStack {
                            Text("Letzter Sync")
                            Spacer()
                            Text(last.formatted(date: .omitted, time: .shortened))
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                Section("Trainingsprofil") {
                    if let p = Binding($profile) {
                        Picker("Ziel", selection: p.goal) {
                            Text("Muskelaufbau").tag("muscle")
                            Text("Kraft").tag("strength")
                            Text("Recomp").tag("recomp")
                        }
                        Picker("Equipment", selection: p.equipment) {
                            Text("Gym").tag("gym")
                            Text("Home").tag("home")
                            Text("Minimal").tag("minimal")
                        }
                        Picker("Erfahrung", selection: p.experience) {
                            Text("Anfänger").tag("beginner")
                            Text("Fortgeschritten").tag("intermediate")
                            Text("Erfahren").tag("advanced")
                        }
                        TextField("Hinweise (Verletzungen, Vorlieben)", text: p.notes, axis: .vertical)
                        Button("Profil speichern") {
                            Task {
                                if let saved = try? await FitnessAPI.shared.saveProfile(p.wrappedValue) {
                                    profile = saved
                                }
                            }
                        }
                    } else {
                        ProgressView()
                    }
                }

                Section("Info") {
                    HStack {
                        Text("Version")
                        Spacer()
                        Text("1.0").foregroundStyle(.secondary)
                    }
                    HStack {
                        Text("Ziel-Kalorien")
                        Spacer()
                        Text("2800 kcal").foregroundStyle(.secondary)
                    }
                    HStack {
                        Text("Ziel-Protein")
                        Spacer()
                        Text("180 g").foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Einstellungen")
            .task { profile = try? await FitnessAPI.shared.fetchProfile() }
        }
    }

    private func testConnection() async {
        isTesting = true
        isConnected = await MantisClient.shared.isReachable
        isTesting = false
    }
}
