from sklearn.ensemble import RandomForestRegressor
import numpy as np, joblib
# Create toy training data with realistic ranges for each feature
rng = np.random.RandomState(0)
X = rng.rand(200,6) * np.array([1000.0, 80.0, 800.0, 30.0, 60.0, 5.0])
y = (
    100 * (
        (1200 - X[:,0]) / 800 * 0.25 +
        (80 - X[:,1]) / 50 * 0.20 +
        (X[:,2] - 100) / 700 * 0.20 +
        1.0 * 0.15 + 1.0 * 0.10 + np.minimum(1.0, X[:,5] / 5.0) * 0.10
    )
).clip(0,100)
model = RandomForestRegressor(n_estimators=20, random_state=0)
model.fit(X, y)
joblib.dump(model, "mood_model.pkl")
print("Wrote mood_model.pkl")
