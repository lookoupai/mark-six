# Mark Six Flutter App

Mobile development is currently paused. Keep the source for future use; normal web/backend work does not require Flutter, Android SDK, or an APK build. Generated build output and temporary SDKs have been removed from the development host.

## Quick start

Only when mobile development is explicitly resumed:

```bash
flutter pub get
flutter run
```

## Backend

Base URL is defined in `lib/config.dart`.

## Release signing (GitHub Actions)

Add these repository secrets:

- `ANDROID_KEYSTORE_BASE64`
- `ANDROID_KEYSTORE_PASSWORD`
- `ANDROID_KEY_ALIAS`
- `ANDROID_KEY_PASSWORD`

The workflow is `Android Release APK`, available only through manual dispatch. Branch, PR, and tag pushes do not build an APK. Routine validation covers the backend and web app; Flutter analysis and tests run only with the manual Android release workflow.
