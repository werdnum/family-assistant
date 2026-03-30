import SwiftUI

struct SetupView: View {
    @Environment(AuthManager.self) private var authManager

    var body: some View {
        @Bindable var auth = authManager

        NavigationStack {
            VStack(spacing: 24) {
                Spacer()

                Image(systemName: "house.fill")
                    .font(.system(size: 64))
                    .foregroundStyle(.tint)

                Text("Family Assistant")
                    .font(.largeTitle.bold())

                Text("Enter your server URL to get started")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)

                VStack(spacing: 16) {
                    TextField("https://fa.example.com", text: $auth.serverURL)
                        .textFieldStyle(.roundedBorder)
                        .textContentType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .onSubmit { signIn() }

                    Button(action: signIn) {
                        if authManager.isLoading {
                            ProgressView()
                                .frame(maxWidth: .infinity)
                        } else {
                            Text("Sign In")
                                .frame(maxWidth: .infinity)
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .disabled(authManager.serverURL.isEmpty || authManager.isLoading)
                }
                .padding(.horizontal)

                if let error = authManager.errorMessage {
                    Text(error)
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal)
                }

                Spacer()
                Spacer()
            }
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private func signIn() {
        authManager.saveServerURL()
        authManager.login()
    }
}
