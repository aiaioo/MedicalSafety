"""Major cities offered in the account page's City drop-down, keyed by the country names in app.COUNTRIES.
Countries without an entry here get a free-text City field; every list is followed by an "Other" choice."""

CITIES_BY_COUNTRY: dict[str, list[str]] = {
    "India": [
        "Agra", "Ahmedabad", "Allahabad (Prayagraj)", "Amritsar", "Aurangabad", "Banaras (Varanasi)",
        "Bangalore (Bengaluru)", "Baroda (Vadodara)", "Bhopal", "Bhubaneswar", "Bombay (Mumbai)",
        "Calcutta (Kolkata)", "Calicut (Kozhikode)", "Chandigarh", "Cochin (Kochi)", "Coimbatore", "Dehradun",
        "Delhi", "Faridabad", "Ghaziabad", "Goa", "Gurugram", "Guwahati", "Gwalior", "Hosur", "Hyderabad",
        "Indore", "Jabalpur", "Jaipur", "Jalandhar", "Jamshedpur", "Jodhpur", "Kannur", "Kanpur", "Lucknow",
        "Ludhiana", "Madras (Chennai)", "Madurai", "Mangalore (Mangaluru)", "Meerut", "Mysuru", "Nagercoil",
        "Nagpur", "Nashik", "Navi Mumbai", "Noida", "Patna", "Pondicherry (Puducherry)", "Pune", "Raipur",
        "Rajkot", "Ranchi", "Srinagar", "Surat", "Thane", "Trichy (Tiruchirappalli)",
        "Trivandrum (Thiruvananthapuram)", "Vijayawada", "Visakhapatnam",
    ],
    "United States": [
        "Atlanta", "Austin", "Baltimore", "Boston", "Charlotte", "Chicago", "Cleveland", "Columbus", "Dallas",
        "Denver", "Detroit", "Houston", "Indianapolis", "Jacksonville", "Kansas City", "Las Vegas", "Los Angeles",
        "Memphis", "Miami", "Milwaukee", "Minneapolis", "Nashville", "New Orleans", "New York", "Oakland",
        "Orlando", "Philadelphia", "Phoenix", "Pittsburgh", "Portland", "Sacramento", "Salt Lake City",
        "San Antonio", "San Diego", "San Francisco", "San Jose", "Seattle", "St. Louis", "Tampa", "Washington, D.C.",
    ],
    "United Kingdom": [
        "Belfast", "Birmingham", "Bradford", "Bristol", "Cardiff", "Coventry", "Edinburgh", "Glasgow", "Leeds",
        "Leicester", "Liverpool", "London", "Manchester", "Newcastle upon Tyne", "Nottingham", "Oxford",
        "Sheffield", "Southampton",
    ],
    "Canada": [
        "Calgary", "Edmonton", "Halifax", "Hamilton", "Kitchener", "London", "Mississauga", "Montreal", "Ottawa",
        "Quebec City", "Regina", "Saskatoon", "Toronto", "Vancouver", "Victoria", "Winnipeg",
    ],
    "Australia": [
        "Adelaide", "Brisbane", "Canberra", "Gold Coast", "Hobart", "Melbourne", "Newcastle", "Perth", "Sydney",
    ],
    "Pakistan": [
        "Faisalabad", "Hyderabad", "Islamabad", "Karachi", "Lahore", "Multan", "Peshawar", "Quetta", "Rawalpindi",
    ],
    "Bangladesh": ["Barisal", "Chattogram", "Dhaka", "Khulna", "Rajshahi", "Sylhet"],
    "Sri Lanka": ["Colombo", "Galle", "Jaffna", "Kandy", "Negombo"],
    "Nepal": ["Biratnagar", "Kathmandu", "Lalitpur", "Pokhara"],
    "Singapore": ["Singapore"],
    "United Arab Emirates": ["Abu Dhabi", "Ajman", "Al Ain", "Dubai", "Ras Al Khaimah", "Sharjah"],
    "Saudi Arabia": ["Dammam", "Jeddah", "Mecca", "Medina", "Riyadh"],
    "Germany": ["Berlin", "Cologne", "Dortmund", "Dresden", "Düsseldorf", "Essen", "Frankfurt", "Hamburg", "Leipzig", "Munich", "Nuremberg", "Stuttgart"],
    "France": ["Bordeaux", "Lille", "Lyon", "Marseille", "Montpellier", "Nantes", "Nice", "Paris", "Strasbourg", "Toulouse"],
    "Ireland": ["Cork", "Dublin", "Galway", "Limerick", "Waterford"],
    "New Zealand": ["Auckland", "Christchurch", "Dunedin", "Hamilton", "Wellington"],
    "South Africa": ["Bloemfontein", "Cape Town", "Durban", "Johannesburg", "Port Elizabeth (Gqeberha)", "Pretoria"],
    "Nigeria": ["Abuja", "Ibadan", "Kano", "Lagos", "Port Harcourt"],
    "Kenya": ["Eldoret", "Kisumu", "Mombasa", "Nairobi", "Nakuru"],
    "Malaysia": ["George Town", "Ipoh", "Johor Bahru", "Kuala Lumpur", "Kuching", "Kota Kinabalu"],
}

# Preselected in the City drop-down when the user has not saved a city yet.
DEFAULT_CITY_BY_COUNTRY: dict[str, str] = {"India": "Bangalore (Bengaluru)"}
