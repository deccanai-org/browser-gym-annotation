// Deploy browser-agent.delta.soulhq.ai (annotation platform).
// Builds backend + frontend Docker images and applies the delta manifest.
// DevOps: set K8S_MANIFEST or wire DEPLOY_CMD to match how browser-agent is hosted today.
pipeline {
    agent { label 'backend-agent' }

    parameters {
        string(name: 'BRANCH_NAME', defaultValue: 'fix/dataset-integrity', description: 'Annotator repo branch')
        string(name: 'VITE_LIVE_BASE', defaultValue: 'https://live-browser.delta.deccanexperts.ai', description: 'Live pane public URL (frontend build arg)')
        choice(name: 'ENV', choices: ['delta', 'prod'], description: 'Target environment')
    }

    environment {
        IMAGE_REGISTRY = 'us-west1-docker.pkg.dev'
        GCP_PROJECT    = 'mlproject-501205'
        IMAGE_REPO     = "${IMAGE_REGISTRY}/${GCP_PROJECT}/annotator"
    }

    stages {
        stage('Checkout') {
            steps {
                git(
                    url: 'https://github.com/deccanai-org/browser-gym-annotation.git',
                    branch: params.BRANCH_NAME,
                    credentialsId: 'jenkins_user_github'
                )
            }
        }

        stage('Set environment') {
            steps {
                script {
                    if (params.ENV == 'prod') {
                        env.K8S_MANIFEST = 'k8s-manifests/prod/browser-agent.yaml'
                        env.FRONTEND_HOST = 'browser-agent.soulhq.ai'
                    } else {
                        env.K8S_MANIFEST = 'k8s-manifests/delta/browser-agent.yaml'
                        env.FRONTEND_HOST = 'browser-agent.delta.soulhq.ai'
                    }
                }
            }
        }

        stage('Build & push images') {
            steps {
                sh '''
                    set -euo pipefail
                    gcloud auth configure-docker ${IMAGE_REGISTRY} --quiet
                    docker build -t ${IMAGE_REPO}/backend:${BUILD_NUMBER} backend/
                    docker build -t ${IMAGE_REPO}/frontend:${BUILD_NUMBER} \
                      --build-arg VITE_LIVE_BASE="${VITE_LIVE_BASE}" frontend/
                    docker push ${IMAGE_REPO}/backend:${BUILD_NUMBER}
                    docker push ${IMAGE_REPO}/frontend:${BUILD_NUMBER}
                '''
            }
        }

        stage('Deploy') {
            steps {
                sh '''
                    set -euo pipefail
                    if [ -f "${K8S_MANIFEST}" ]; then
                      export BACKEND_IMAGE="${IMAGE_REPO}/backend:${BUILD_NUMBER}"
                      export FRONTEND_IMAGE="${IMAGE_REPO}/frontend:${BUILD_NUMBER}"
                      envsubst < "${K8S_MANIFEST}" | kubectl apply -f -
                    elif [ -n "${BROWSER_AGENT_DEPLOY_CMD:-}" ]; then
                      eval "${BROWSER_AGENT_DEPLOY_CMD}"
                    else
                      echo "!! No ${K8S_MANIFEST} and BROWSER_AGENT_DEPLOY_CMD unset."
                      echo "   Images built and pushed — DevOps must wire deploy."
                      exit 1
                    fi
                '''
            }
        }

        stage('Smoke') {
            steps {
                sh '''
                    set -e
                    curl -fsS -o /dev/null -w "frontend %{http_code}\n" "https://${FRONTEND_HOST}/"
                    curl -fsS -o /dev/null -w "api health %{http_code}\n" "https://${FRONTEND_HOST}/api/health" || true
                '''
            }
        }
    }
}
