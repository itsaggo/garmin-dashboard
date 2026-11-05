from app import extract


def test_records_descends_into_dated_wrapper():
    body = {"date": "2026-11-01", "items": [
        {"calendarDate": "2026-10-30", "totalSteps": 1},
        {"calendarDate": "2026-10-31", "totalSteps": 2},
    ]}
    assert [d for d, _ in extract.records(body)] == ["2026-10-30", "2026-10-31"]


def test_records_keeps_dated_record_with_nested_arrays():
    body = [{"date": "2026-11-01", "bodyBatteryValuesArray": [[1, 20], [2, 80]]}]
    rows = list(extract.body_battery(body))
    assert rows == [("2026-11-01", {"bb_high": 80, "bb_low": 20})]


def test_stress_negative_means_no_data():
    rows = dict(extract.stress([{"calendarDate": "2026-11-01", "values": {"overallStressLevel": -1}}]))
    assert "avg_stress" not in rows["2026-11-01"]


def test_sleep_nested_dto():
    body = {"dailySleepSummaries": [{"dailySleepDTO": {
        "calendarDate": "2026-11-02", "sleepTimeSeconds": 25200, "deepSleepSeconds": 3600,
        "sleepScores": {"overall": {"value": 81}}}}]}
    (day, row), = list(extract.sleep(body))
    assert day == "2026-11-02" and row["sleep_s"] == 25200 and row["deep_s"] == 3600 and row["sleep_score"] == 81


def test_activity_parsing():
    aid, row = extract.activity({"activityId": "42", "activityName": "Easy Run", "startTimeLocal": "2026-11-03T06:30:00",
                                 "activityType": {"typeKey": "running"}, "distance": 8046.7, "averageHR": 150})
    assert aid == 42 and row["type"] == "running" and row["start_local"] == "2026-11-03 06:30:00"
    assert row["distance_m"] == 8046.7 and row["avg_hr"] == 150


def test_activity_without_id_is_skipped():
    assert extract.activity({"activityName": "x"}) == (None, {})


# ---- Garmin Connect shapes (from verified fixtures) ----

def test_vo2max_range():
    body = [{"cycling": None, "generic": {"calendarDate": "2025-12-10", "fitnessAge": None, "maxMetCategory": 0,
                                          "vo2MaxPreciseValue": 53.4, "vo2MaxValue": 53.0},
             "heatAltitudeAcclimation": {"acclimationPercentage": 0}, "userId": 1}]
    assert list(extract.vo2max(body)) == [("2025-12-10", {"vo2max": 53.4})]


def test_vo2max_falls_back_to_rounded_value_and_reads_cycling():
    body = [{"generic": {"calendarDate": "2026-01-01", "vo2MaxValue": 50},
             "cycling": {"calendarDate": "2026-01-01", "vo2MaxPreciseValue": 48.2}}]
    assert list(extract.vo2max(body)) == [("2026-01-01", {"vo2max": 50}), ("2026-01-01", {"vo2max_cycling": 48.2})]


def test_readiness_morning_score_latest_recovery():
    body = [
        {"calendarDate": "2025-12-13", "timestampLocal": "2025-12-13T18:00:00", "score": 40, "level": "LOW",
         "recoveryTime": 900, "inputContext": "UPDATE_REALTIME_VARIABLES"},
        {"calendarDate": "2025-12-13", "timestampLocal": "2025-12-13T06:40:00", "score": 69, "level": "MODERATE",
         "feedbackShort": "WELL_RECOVERED", "recoveryTime": 273, "inputContext": "AFTER_WAKEUP_RESET"},
    ]
    (day, row), = list(extract.readiness(body))
    assert day == "2025-12-13"
    assert row["readiness_score"] == 69 and row["readiness_level"] == "Moderate"
    assert row["readiness_feedback"] == "Well recovered"
    assert row["recovery_min"] == 900 and row["readiness_time"] == "2025-12-13 18:00"


def test_readiness_reached_zero_means_recovered():
    body = [{"calendarDate": "2026-11-01", "timestampLocal": "2026-11-01T07:00:00", "score": 80,
             "recoveryTime": 120, "recoveryTimeChangePhrase": "REACHED_ZERO"}]
    assert dict(extract.readiness(body))["2026-11-01"]["recovery_min"] == 0


def test_training_status_aggregated():
    body = {
        "mostRecentVO2Max": {"generic": {"calendarDate": "2025-12-12", "vo2MaxValue": 53.0}},
        "mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {
            "123": {"trainingBalanceFeedbackPhrase": "ANAEROBIC_SHORTAGE", "primaryTrainingDevice": True}}},
        "mostRecentTrainingStatus": {"latestTrainingStatusData": {
            "999": {"calendarDate": "2025-12-01", "trainingStatusFeedbackPhrase": "PEAKING_1", "primaryTrainingDevice": False},
            "123": {"calendarDate": "2025-12-14", "trainingStatusFeedbackPhrase": "UNPRODUCTIVE_4",
                    "primaryTrainingDevice": True,
                    "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 268, "dailyTrainingLoadChronic": 240,
                                             "dailyAcuteChronicWorkloadRatio": 1.1, "acwrStatus": "OPTIMAL"}},
        }},
    }
    rows = list(extract.training_status(body))
    assert rows[0] == ("2025-12-14", {"training_status": "Unproductive", "acute_load": 268, "chronic_load": 240,
                                      "acwr": 1.1, "acwr_status": "Optimal", "load_balance": "Anaerobic shortage"})
    assert rows[1] == ("2025-12-12", {"vo2max": 53.0})


def test_training_status_empty_and_garbage():
    assert list(extract.training_status({})) == []
    assert list(extract.training_status(None)) == []
    assert list(extract.training_status({"mostRecentTrainingStatus": {"latestTrainingStatusData": "x"}})) == []
    assert list(extract.readiness("nonsense")) == []
    assert list(extract.vo2max([None, 3, "x"])) == []


def test_phrase():
    assert extract._phrase("PRODUCTIVE_1") == "Productive"
    assert extract._phrase("NO_STATUS") == "No status"
    assert extract._phrase("") is None and extract._phrase(None) is None
