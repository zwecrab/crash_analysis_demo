# Prediction Model API Contract

This document defines the API that the prediction model should implement.

## Endpoint

```
POST /api/predict
Content-Type: application/json
```

## Request Schema

```json
{
  "vehicles": [
    {
      "vin": "VRD00000000072990",
      "lat": 13.842576,
      "lon": 100.559620,
      "speed": 45,
      "direction": 180,
      "recent_events": [1, 2]
    }
  ],
  "area": {
    "lat_min": 13.839,
    "lat_max": 13.846,
    "lon_min": 100.555,
    "lon_max": 100.561
  },
  "timestamp": "2025-02-15T12:00:00"
}
```

## Response Schema

```json
{
  "predictions": [
    {
      "lat": 13.842,
      "lon": 100.559,
      "risk": 0.85,
      "label": "High collision risk - front-back",
      "confidence": 0.72
    }
  ],
  "model_ready": true,
  "model_version": "1.0.0"
}
```

## Fields

| Field | Type | Description |
|-------|------|-------------|
| `risk` | float 0-1 | Predicted collision probability |
| `label` | string | Human-readable risk description |
| `confidence` | float 0-1 | Model confidence in prediction |
| `model_ready` | bool | False until model is connected |

## Integration Steps

1. Replace the stub in `app.py` (`POST /api/predict`) with your model inference
2. Set `model_ready: True` in the response
3. The frontend will automatically enable the "Predictions" toggle
4. Predictions appear as heatmap overlay or warning markers on the map

## Data Available

- **Event types**: 1=Sudden Acceleration, 2=Harsh Braking, 3=Sharp Turn
- **Collision type**: 17=Front-Back Collision (Driving)
- **Speed**: km/h (NULL for ~77% of records)
- **G-forces**: gx_acci, gy_acci (only during collisions)
- **Direction**: 0-359 degrees, 0=North clockwise
