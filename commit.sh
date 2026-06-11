echo "Committing changes for agentC..."
cd ~/repo/agentC
git pull
git add .
git commit -m "Auto-commit"
git push
echo "Committing changes for bugbounty..."
cd ~/repo/agentC/bugbounty
git add .
git commit -m "Auto-commit"
cd ~/repo/agentC
echo "commit.sh run complete"
