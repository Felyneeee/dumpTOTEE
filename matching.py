import numpy as np
from scipy.optimize import linear_sum_assignment
from geopy.distance import geodesic

# Approximate coordinates for Cavite cities and municipalities (Latitude, Longitude)
CAVITE_COORDINATES = {
    'bacoor': (14.4608, 120.9631),
    'cavite city': (14.4831, 120.8986),
    'dasmarinas': (14.3294, 120.9367),
    'general trias': (14.3861, 120.8806),
    'imus': (14.4297, 120.9367),
    'tagaytay': (14.1153, 120.9621),
    'trece martires': (14.2828, 120.8672),
    'alfonso': (14.1378, 120.8542),
    'amadeo': (14.1700, 120.9239),
    'carmona': (14.3160, 121.0583),
    'general emilio aguinaldo': (14.1842, 120.7933),
    'indang': (14.1953, 120.8769),
    'kawit': (14.4442, 120.9025),
    'maragondon': (14.2764, 120.7375),
    'mendez': (14.1297, 120.9078),
    'naic': (14.3181, 120.7675),
    'noveleta': (14.4278, 120.8783),
    'rosario': (14.4150, 120.8586),
    'silang': (14.2289, 120.9744),
    'tanza': (14.3589, 120.8528),
    'ternate': (14.2881, 120.7183)
}

def get_location_coords(location_name):
    """
    Look up predefined coordinates for Cavite locations.
    """
    key = location_name.strip().lower()
    return CAVITE_COORDINATES.get(key, None)

def calculate_location_score(tutee_loc, tutor_loc, modality):
    """
    Evaluates location compatibility using Geopy distance in kilometers:
    - 10.0 pts: Distance <= 5 km
    - 10.0 to 2.5 pts: Distance between 5 km and 25 km (linearly scaled)
    - 5.0 pts: Online teaching modality fallback
    - 0.0 pts: Unmatched / > 25 km in-person
    """
    loc1 = tutee_loc.strip().lower()
    loc2 = tutor_loc.strip().lower()

    # Exact name match
    if loc1 == loc2:
        return 10.0

    coords1 = get_location_coords(loc1)
    coords2 = get_location_coords(loc2)

    if coords1 and coords2:
        # Calculate geodesic distance in kilometers
        dist_km = geodesic(coords1, coords2).kilometers

        if dist_km <= 5.0:
            return 10.0
        elif dist_km <= 25.0:
            # Scale score smoothly from 10 down to 2.5 points based on distance
            return round(10.0 - ((dist_km - 5.0) / 20.0) * 7.5, 2)

    # Online Modality Fallback
    if modality.strip().lower() == 'online':
        return 5.0

    return 0.0

def calculate_match_score(tutee, tutor):
    """
    Calculates a score from 0 to 100 based on matching criteria:
    - Subject Expertise (Strict requirement)
    - Schedule Availability (Strict requirement)
    - Budget / Rate compatibility
    - Location matching (Geodesic distance calculation via Geopy)
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
    elif tutee_budget >= (tutor_rate * 0.8):
        score += 10.0

    # 4. Teaching Modality (15%)
    if tutee['modality'].strip().lower() == tutor['modality'].strip().lower() or tutor['modality'].lower() == 'both':
        score += 15.0

    # 5. Geopy Geodesic Location Distance Matching (10%)
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
