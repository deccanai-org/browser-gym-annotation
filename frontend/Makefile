DEV_ARGS = VITE_ENV=development \
           VITE_DOMAIN=deccanexperts.ai \
           VITE_LIVE_BASE=https://live-browser.delta.deccanexperts.ai

PROD_ARGS = VITE_ENV=production \
            VITE_DOMAIN=deccanexperts.ai \
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
	@echo "⚙️  Running yarn build..."
	@$(DEV_ARGS) yarn build
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
	@echo "⚙️  Running yarn build..."
	@$(PROD_ARGS) yarn build
	@echo ""
	@echo "✅ Build completed successfully for Deccan (Production)"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

