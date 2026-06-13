echo "Committing changes for agentC..."
cd ~/repo/agentC
git pull
git add .
git commit -m "Auto-commit"
git push
echo "git_commit.sh run complete"
