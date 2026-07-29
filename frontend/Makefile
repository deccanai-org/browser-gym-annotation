# Frontend build for the deployment pipeline. Only VITE_-prefixed vars reach the
# browser bundle. VITE_ROOT is the auth backend (Google ID token -> session);
# VITE_GOOGLE_CLIENT_ID is the OAuth client the token is minted for.
GOOGLE_CLIENT_ID = 908367891804-cohtn6o8akmjdubh1gp4fgbq49ra3ldl.apps.googleusercontent.com

DEV_ARGS = VITE_ENV=development \
           VITE_DOMAIN=deccanexperts.ai \
           VITE_ROOT=https://usersvc.delta.deccanexperts.ai \
           VITE_GOOGLE_CLIENT_ID=$(GOOGLE_CLIENT_ID) \
           VITE_LIVE_BASE=https://live-browser.delta.deccanexperts.ai

PROD_ARGS = VITE_ENV=production \
            VITE_DOMAIN=deccanexperts.ai \
            VITE_ROOT=https://api.deccanexperts.ai \
            VITE_GOOGLE_CLIENT_ID=$(GOOGLE_CLIENT_ID) \
            VITE_LIVE_BASE=https://live-browser.deccanexperts.ai


# Build Targets for Different Environments
build-dev:
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo "🚀 Starting build for Deccan (Development) 🚀"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo "📍 Environment: Development"
	@echo "🏷️  Brand: Deccan"
	@echo "🔗 API Root: https://usersvc.delta.deccanexperts.ai"
	@echo "🌐 Host URL: https://studio.delta.deccanexperts.ai"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo "⚙️  Running npm run build..."
	@$(DEV_ARGS) npm run build
	@echo ""
	@echo "✅ Build completed successfully for Deccan (Development)"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

build-prod:
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo "🚀 Starting build for Deccan (Production) 🚀"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo "📍 Environment: Production"
	@echo "🏷️  Brand: Deccan"
	@echo "🔗 API Root: https://api.deccanexperts.ai"
	@echo "🌐 Host URL: https://studio.deccanexperts.ai"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo "⚙️  Running npm run build..."
	@$(PROD_ARGS) npm run build
	@echo ""
	@echo "✅ Build completed successfully for Deccan (Production)"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
