import numpy as np
from scipy.optimize import linear_sum_assignment

# Map of neighboring/adjacent cities and municipalities (e.g., Cavite & nearby areas)
NEARBY_LOCATIONS = {
    'imus': ['bacoor', 'dasmarinas', 'kawit', 'general trias'],
    'bacoor': ['imus', 'las pinas', 'kawit', 'paranaque'],
    'dasmarinas': ['imus', 'general trias', 'silang', 'carmona'],
    'kawit': ['imus', 'bacoor', 'noveleta', 'cavite city'],
    'general trias': ['imus', 'dasmarinas', 'trece martires', 'kawit', 'amadeo'],
    'silang': ['dasmarinas', 'tagaytay', 'general trias', 'carmona'],
    'trece martires': ['general trias', 'tanza', 'naic', 'amadeo'],
    'las pinas': ['bacoor', 'paranaque', 'muntinlupa'],
    'paranaque': ['las pinas', 'pasay', 'taguig', 'bacoor']
}

def calculate_location_score(tutee_loc, tutor_loc, modality):
    """
    Evaluates location compatibility:
    - 10.0 pts: Exact same city/location
    - 7.5 pts: Adjacent / nearby neighboring city
    - 5.0 pts: Online teaching modality fallback
    - 0.0 pts: Unmatched & far location
    """
    loc1 = tutee_loc.strip().lower()
    loc2 = tutor_loc.strip().lower()

    # 1. Exact Match
    if loc1 == loc2:
        return 10.0

    # 2. Check if tutor's location is in tutee's neighboring list (or vice versa)
    if loc2 in NEARBY_LOCATIONS.get(loc1, []) or loc1 in NEARBY_LOCATIONS.get(loc2, []):
        return 7.5

    # 3. Online Modality Fallback
    if modality.strip().lower() == 'online':
        return 5.0

    # 4. Far / Unmatched
    return 0.0

def calculate_match_score(tutee, tutor):
    """
    Calculates a score from 0 to 100 based on matching criteria:
    - Subject Expertise (Strict requirement)
    - Schedule Availability (Strict requirement)
    - Budget / Rate compatibility
    - Location matching (Exact vs. Nearby vs. Online)
    - Teaching Modality (Online vs In-Person)
    """
    score = 0.0

    # 1. Subject Expertise (Strict: 30%)
    if tutee['subject'].strip().lower() == tutor['subject'].strip().lower():
        score += 30.0
    else:
        return 0.0  # Hard constraint failure

    # 2. Schedule Availability (Strict: 25%)
    if tutee['schedule'].strip().lower() == tutor['schedule'].strip().lower():
        score += 25.0
    else:
        return 0.0  # Hard constraint failure

    # 3. Budget & Rate Compatibility (20%)
    tutee_budget = float(tutee['budget'])
    tutor_rate = float(tutor['rate'])
    if tutee_budget >= tutor_rate:
        score += 20.0
    else:
        if tutee_budget >= (tutor_rate * 0.8):
            score += 10.0

    # 4. Teaching Modality (15%)
    if tutee['modality'].strip().lower() == tutor['modality'].strip().lower() or tutor['modality'].lower() == 'both':
        score += 15.0

    # 5. Location Matching (10%) - Now supports nearby neighbors like Imus & Bacoor!
    score += calculate_location_score(tutee['location'], tutor['location'], tutee['modality'])

    return score

def find_best_match_hungarian(tutee, available_tutors):
    if not available_tutors:
        return None, 0.0

    scores = []
    for tutor in available_tutors:
        score = calculate_match_score(tutee, tutor)
        scores.append(score)

    max_score = max(scores) if scores else 0
    if max_score <= 0:
        return None, 0.0

    cost_matrix = np.array([[100.0 - s for s in scores]])
    row_ind, col_ind = linear_sum_assignment(cost_matrix)

    best_tutor_idx = col_ind[0]
    best_tutor = available_tutors[best_tutor_idx]
    best_score = scores[best_tutor_idx]

    return best_tutor, round(best_score, 1)