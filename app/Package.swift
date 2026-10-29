// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "VolfreeApp",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "VolfreeApp",
            path: "Sources/VolfreeApp"
        )
    ]
)
